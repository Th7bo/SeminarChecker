#!/usr/bin/env python3
"""
PXL Seminar reminder: checks seminar pages periodically and sends a Discord
message (as a bot) when the "Inschrijven" (register) link is no longer '#'
(registration open). Only one notification per seminar is sent.
"""

import asyncio
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import discord
import requests
from bs4 import BeautifulSoup

from db import (
    get_notified_count,
    get_notified_seminar_ids,
    get_status_message_id,
    init_db,
    mark_notified,
    set_status_message_id,
)

BASE_URL = "https://pxl-digital.pxl.be"
SEMINARIES_LIST_URL = f"{BASE_URL}/i-talent/seminaries-2tin-25-26"
USER_AGENT = "PXL-Seminar-Reminder/1.0"

log = logging.getLogger("seminar_reminder")


def setup_logging(level: str = None) -> None:
    """Configure logging. Level from env LOG_LEVEL or argument (default: INFO)."""
    lvl = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=getattr(logging, lvl, logging.INFO),
        stream=sys.stdout,
    )
    log.setLevel(getattr(logging, lvl, logging.INFO))


def get_seminar_links_from_list_page(html: str) -> list[str]:
    """Parse main seminaries page and return absolute URLs of each 'meer info' seminar page."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        if "meer info" not in (a.get_text() or "").strip().lower():
            continue
        href = (a["href"] or "").strip()
        if not href or href == "#":
            continue
        if not href.startswith("http"):
            href = BASE_URL.rstrip("/") + ("/" if not href.startswith("/") else "") + href
        # Normalize: one entry per seminar (avoid duplicates from same path)
        if href not in links:
            links.append(href)
    return links


def normalize_seminar_id(url: str) -> str:
    """Stable ID for a seminar (for deduplication)."""
    url = url.split("?")[0].rstrip("/")
    if url.startswith(BASE_URL):
        return url
    return f"{BASE_URL}{url}" if url.startswith("/") else url


def fetch_seminar_page(url: str) -> str | None:
    """Fetch seminar detail page HTML. Returns None on failure."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        log.debug("Fetch failed for %s: %s", url, e)
        return None


def parse_seminar_page(html: str, page_url: str) -> dict | None:
    """
    Parse seminar detail page. Returns dict with title, subtitle, register_url, and other
    fields for Discord embed. register_url is None if Inschrijven is still '#' (not open).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find Inschrijven link
    inschrijven_url = None
    for a in soup.find_all("a", href=True):
        if (a.get_text() or "").strip() == "Inschrijven":
            href = (a["href"] or "").strip()
            if href and href != "#":
                inschrijven_url = href if href.startswith("http") else BASE_URL.rstrip("/") + ("/" if not href.startswith("/") else "") + href
            break

    # Only care about seminars where registration is open (for notification)
    # We still return data so caller can decide to notify only when inschrijven_url is set

    # Title: first h1 in main (e.g. "Seminarie: IBM")
    main = soup.find("main")
    title = None
    subtitle = None
    if main:
        h1 = main.find("h1")
        if h1:
            title = (h1.get_text() or "").strip()
        lead = main.find("p", class_="lead")
        if lead:
            # First non-empty font or text
            for font in lead.find_all("font"):
                t = (font.get_text() or "").strip()
                if t:
                    subtitle = t
                    break
            if subtitle is None:
                subtitle = (lead.get_text() or "").strip() or None

    # Features section: Bedrijf, Specialisatie, Praktisch (date/time/location), etc.
    company = None
    practical = None
    specialisation = None
    features = main.find("section", class_=re.compile(r"s_features")) if main else None
    if features:
        rows = features.find_all("div", class_="text-center")
        for div in rows:
            h3 = div.find("h3")
            if not h3:
                continue
            label = (h3.get_text() or "").strip()
            p = div.find("p")
            value = (p.get_text() or "").strip().replace("\n", " ") if p else ""
            if label == "Bedrijf":
                company = value or None
            elif label == "Specialisatie":
                specialisation = value or None
            elif label == "Praktisch":
                practical = value or None

    return {
        "url": page_url,
        "title": title or "Seminar",
        "subtitle": subtitle,
        "register_url": inschrijven_url,
        "company": company,
        "specialisation": specialisation,
        "practical": practical,
    }


def get_seminar_year(seminar: dict) -> int | None:
    """
    Extract the seminar's event year from register URL (e.g. ...-2025-02-25-.../register)
    or from practical text (e.g. "24 maart 2026"). Returns None if unknown.
    """
    # Prefer register URL: /event/...-YYYY-MM-DD-.../register
    url = seminar.get("register_url") or seminar.get("url") or ""
    m = re.search(r"-(\d{4})-\d{2}-\d{2}-", url)
    if m:
        return int(m.group(1))
    # Fallback: 4-digit year in practical (e.g. "24 maart 2026")
    practical = seminar.get("practical") or ""
    m = re.search(r"\b(20\d{2})\b", practical)
    if m:
        return int(m.group(1))
    return None


def build_discord_embed(seminar: dict) -> dict:
    """Build a Discord embed payload for one seminar."""
    title = seminar.get("title") or "Seminar"
    if seminar.get("subtitle"):
        title = f"{title}: {seminar['subtitle']}"

    # Discord embed field values must be non-empty and ≤ 1024 chars
    def field_value(s: str | None, max_len: int = 1024) -> str:
        if not s or not (s := s.strip()):
            return "\u200b"
        return (s[: max_len - 3] + "...") if len(s) > max_len else s

    fields = []
    if seminar.get("company"):
        fields.append({"name": "Bedrijf", "value": field_value(seminar["company"]), "inline": True})
    if seminar.get("specialisation"):
        fields.append({"name": "Specialisatie", "value": field_value(seminar["specialisation"]), "inline": True})
    if seminar.get("practical"):
        fields.append({"name": "Praktisch", "value": field_value(seminar["practical"]), "inline": False})

    embed = {
        "title": title[:256],
        "url": seminar.get("register_url") or seminar.get("url"),
        "color": 0x5865F2,
        "fields": fields,
        "footer": {"text": "PXL-Digital Seminaries 2TIN"},
    }
    if seminar.get("register_url"):
        embed["description"] = "**Inschrijven is open!** Klik op de titel om te registreren."
    return embed


def _embed_dict_to_discord(embed_dict: dict) -> discord.Embed:
    """Convert our embed dict to a discord.Embed."""
    e = discord.Embed(
        title=embed_dict.get("title", "Seminar")[:256],
        description=embed_dict.get("description"),
        url=embed_dict.get("url"),
        color=embed_dict.get("color", 0x5865F2),
    )
    for f in embed_dict.get("fields", []):
        e.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    if embed_dict.get("footer", {}).get("text"):
        e.set_footer(text=embed_dict["footer"]["text"])
    return e


async def _send_discord_message_async(
    bot_token: str,
    channel_id: int,
    seminar: dict,
    ping: str,
) -> None:
    """Send one message via discord.py (async). Raises on failure."""
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = await client.fetch_channel(channel_id)
            if channel is None:
                raise ValueError(f"Channel {channel_id} not found")
            embed_dict = build_discord_embed(seminar)
            embed = _embed_dict_to_discord(embed_dict)
            content = ping.strip() if ping and ping.strip() else None
            await channel.send(content=content, embed=embed)
        finally:
            await client.close()

    async with client:
        await client.start(bot_token)


def send_discord_message(
    bot_token: str,
    channel_id: str,
    seminar: dict,
    ping: str = "@everyone",
) -> bool:
    """Send one Discord message (as bot) to the given channel with embed. Returns True on success."""
    try:
        asyncio.run(
            _send_discord_message_async(
                bot_token,
                int(channel_id),
                seminar,
                ping or "",
            )
        )
        return True
    except discord.DiscordException as e:
        log.warning("Discord error: %s", e)
        return False
    except Exception as e:
        log.warning("Discord send failed: %s", e)
        return False


def _discord_timestamp(dt: datetime, style: str = "f") -> str:
    """Format a datetime as Discord's relative timestamp. Style: t, T, d, D, f, F, R."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def build_status_embed(
    *,
    seminaries_on_list: int,
    open_for_registration: int,
    total_notified: int,
    new_this_run: int,
    last_check: datetime,
    next_update_utc: datetime | None = None,
) -> discord.Embed:
    """Build the global status embed (edited in place each run)."""
    e = discord.Embed(
        title="PXL Seminar Reminder – Status",
        color=0x5865F2,
        timestamp=last_check,
    )
    e.add_field(
        name="Seminaries on list",
        value=str(seminaries_on_list),
        inline=True,
    )
    e.add_field(
        name="Open for registration",
        value=str(open_for_registration),
        inline=True,
    )
    e.add_field(
        name="Total notified (all time)",
        value=str(total_notified),
        inline=True,
    )
    e.add_field(
        name="New this run",
        value=str(new_this_run),
        inline=True,
    )
    if next_update_utc is not None:
        # Discord time: :R = relative ("in 1 hour"), :f = short date/time (locale)
        e.add_field(
            name="Next update",
            value=f"{_discord_timestamp(next_update_utc, 'R')} ({_discord_timestamp(next_update_utc, 'f')})",
            inline=False,
        )
    e.set_footer(text="PXL-Digital Seminaries 2TIN · Last check")
    return e


async def _update_status_message_async(
    bot_token: str,
    channel_id: int,
    embed: discord.Embed,
) -> None:
    """Create or edit the single status message. Uses stored message ID to edit in place."""
    stored_id = get_status_message_id()
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        try:
            channel = await client.fetch_channel(channel_id)
            if channel is None:
                raise ValueError(f"Channel {channel_id} not found")
            if stored_id:
                try:
                    message = await channel.fetch_message(int(stored_id))
                    await message.edit(embed=embed, content=None)
                    log.debug("Edited status message %s", stored_id)
                except discord.NotFound:
                    msg = await channel.send(embed=embed)
                    set_status_message_id(str(msg.id))
                    log.info("Status message was deleted; recreated as %s", msg.id)
            else:
                msg = await channel.send(embed=embed)
                set_status_message_id(str(msg.id))
                log.debug("Created status message %s", msg.id)
        finally:
            await client.close()

    async with client:
        await client.start(bot_token)


def update_status_message(
    bot_token: str,
    channel_id: str,
    embed: discord.Embed,
) -> bool:
    """Create or edit the status embed message. Returns True on success."""
    try:
        asyncio.run(_update_status_message_async(bot_token, int(channel_id), embed))
        return True
    except discord.DiscordException as e:
        log.warning("Discord status update error: %s", e)
        return False
    except Exception as e:
        log.warning("Status message update failed: %s", e)
        return False


def run_check(
    bot_token: str | None = None,
    channel_id: str | None = None,
    ping: str | None = None,
    interval_minutes: float | None = None,
) -> None:
    """Fetch seminar list, check each detail page for open registration, notify once per seminar."""
    bot_token = bot_token or os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = channel_id or os.environ.get("DISCORD_CHANNEL_ID")
    if not bot_token:
        log.error("Set DISCORD_BOT_TOKEN or pass --bot-token")
        sys.exit(1)
    if not channel_id:
        log.error("Set DISCORD_CHANNEL_ID or pass --channel")
        sys.exit(1)
    ping = (ping if ping is not None else os.environ.get("DISCORD_PING", "@everyone")) or ""

    log.info("Starting seminar check (list=%s)", SEMINARIES_LIST_URL)
    init_db()
    notified = get_notified_seminar_ids()
    current_year = date.today().year
    log.info("Current year=%d, already notified=%d seminar(s)", current_year, len(notified))

    try:
        r = requests.get(
            SEMINARIES_LIST_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to fetch seminar list: %s", e)
        sys.exit(1)

    seminar_urls = get_seminar_links_from_list_page(r.text)
    log.info("Fetched list page, found %d seminar link(s)", len(seminar_urls))

    open_count = 0
    new_notifications = 0
    for url in seminar_urls:
        sid = normalize_seminar_id(url)
        log.debug("Checking seminar: %s", url)
        html = fetch_seminar_page(url)
        if not html:
            log.debug("Skip (fetch failed): %s", url)
            continue
        data = parse_seminar_page(html, url)
        if not data:
            log.debug("Skip (parse failed): %s", url)
            continue
        if not data.get("register_url"):
            log.debug("Skip (registration not open): %s", data.get("title", url))
            continue
        seminar_year = get_seminar_year(data)
        if seminar_year is not None and seminar_year != current_year:
            log.debug("Skip (year %s != %s): %s", seminar_year, current_year, data.get("title", url))
            continue
        open_count += 1
        if sid in notified:
            log.debug("Skip (already notified): %s", url)
            continue
        log.info("Sending notification: %s", data.get("title", url))
        if send_discord_message(bot_token, channel_id, data, ping):
            mark_notified(sid, seminar_url=url, title=data.get("title"))
            notified.add(sid)
            new_notifications += 1
            log.info("Notified: %s", data.get("title", url))
        else:
            log.warning("Discord send failed for: %s", data.get("title", url))

    total = get_notified_count()
    log.info("Done. Notified %d new seminar(s) this run. State has %d total.", new_notifications, total)

    # Update the single global status embed (create once, then edit in place)
    last_check = datetime.now(timezone.utc)
    next_update_utc = (
        last_check + timedelta(minutes=max(1, interval_minutes))
        if interval_minutes is not None
        else None
    )
    status_embed = build_status_embed(
        seminaries_on_list=len(seminar_urls),
        open_for_registration=open_count,
        total_notified=total,
        new_this_run=new_notifications,
        last_check=last_check,
        next_update_utc=next_update_utc,
    )
    if update_status_message(bot_token, channel_id, status_embed):
        log.debug("Status embed updated")
    else:
        log.warning("Failed to update status embed")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Check PXL seminars and notify Discord (as a bot) when registration opens.")
    parser.add_argument("--bot-token", "-t", default=os.environ.get("DISCORD_BOT_TOKEN"), help="Discord bot token (or set DISCORD_BOT_TOKEN)")
    parser.add_argument("--channel", "-c", default=os.environ.get("DISCORD_CHANNEL_ID"), help="Discord channel ID to post to (or set DISCORD_CHANNEL_ID)")
    parser.add_argument("--ping", "-p", default=os.environ.get("DISCORD_PING", "@everyone"), help="Mention to ping (e.g. @everyone, @here, or <@&role_id>). Default: @everyone. Set empty to disable.")
    parser.add_argument("--loop", "-l", action="store_true", help="Run indefinitely, re-check every N minutes")
    parser.add_argument("--interval", "-i", type=float, default=60, help="Minutes between checks when using --loop (default: 60)")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"), choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="Logging level (default: INFO, or env LOG_LEVEL)")
    args = parser.parse_args()
    setup_logging(args.log_level)
    if not args.bot_token:
        log.error("Set DISCORD_BOT_TOKEN or pass --bot-token")
        sys.exit(1)
    if not args.channel:
        log.error("Set DISCORD_CHANNEL_ID or pass --channel")
        sys.exit(1)
    if not os.environ.get("DATABASE_URL"):
        log.error("DATABASE_URL is not set (required for PostgreSQL)")
        sys.exit(1)
    if args.loop:
        import time
        while True:
            run_check(
                bot_token=args.bot_token,
                channel_id=args.channel,
                ping=args.ping,
                interval_minutes=args.interval,
            )
            time.sleep(max(60, args.interval * 60))
    else:
        interval = os.environ.get("CHECK_INTERVAL")
        run_check(
            bot_token=args.bot_token,
            channel_id=args.channel,
            ping=args.ping,
            interval_minutes=float(interval) if interval else None,
        )


if __name__ == "__main__":
    main()
