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


def check_register_page_available(register_url: str) -> bool:
    """
    Fetch the register page and return True if it looks available (proper event page, registration open).
    Return False if the page is "Webapplicatie niet beschikbaar", "not available", or "Registraties gesloten".
    """
    try:
        r = requests.get(
            register_url,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        html = r.text.lower()
    except requests.RequestException as e:
        log.debug("Register page fetch failed for %s: %s", register_url, e)
        return False
    # "Web application not available" / "Webapplicatie niet beschikbaar" (failed.html)
    if "niet beschikbaar" in html or "not available" in html and "web application" in html:
        log.info("Register page not available (blocked/unavailable): %s", register_url)
        return False
    # "Registraties zijn gesloten" / "Registraties gesloten" (registration closed)
    if "registraties" in html and "gesloten" in html:
        log.info("Register page shows registrations closed: %s", register_url)
        return False
    return True


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
        if seminar.get("register_available", True):
            embed["description"] = "**Inschrijven is open!** Klik op de titel om te registreren."
        else:
            embed["description"] = "**Inschrijven-link gevonden, maar** de registratiepagina is niet beschikbaar of gesloten. Geen ping."
            embed["color"] = 0xEDB90B  # orange/warning
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


async def _send_seminar_embed_async(channel: discord.abc.Messageable, seminar: dict, ping: str) -> bool:
    """Send one seminar notification. Returns True on success."""
    try:
        embed = _embed_dict_to_discord(build_discord_embed(seminar))
        content = ping.strip() if ping and ping.strip() else None
        await channel.send(content=content, embed=embed)
        return True
    except discord.DiscordException as e:
        log.warning("Discord error: %s", e)
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


async def _update_status_with_bot(
    bot: discord.Client,
    channel_id: int,
    embed: discord.Embed,
) -> None:
    """Create or edit the status message using the running bot."""
    channel = await bot.fetch_channel(channel_id)
    if channel is None:
        raise ValueError(f"Channel {channel_id} not found")
    stored_id = get_status_message_id()
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


def run_check_compute(
    interval_minutes: float,
) -> tuple[list[tuple[str, str, dict]], int, int]:
    """
    Sync: fetch list, parse seminars, return (to_notify, open_count, seminaries_on_list).
    to_notify = [(sid, url, data), ...] for open + not yet notified. No Discord I/O.
    """
    init_db()
    notified = get_notified_seminar_ids()
    current_year = date.today().year
    log.info("Seminar check: year=%d, already notified=%d", current_year, len(notified))

    try:
        r = requests.get(
            SEMINARIES_LIST_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to fetch seminar list: %s", e)
        return [], 0, 0

    seminar_urls = get_seminar_links_from_list_page(r.text)
    log.info("Fetched list page, found %d seminar link(s)", len(seminar_urls))
    if not seminar_urls:
        return [], 0, 0

    to_notify: list[tuple[str, str, dict]] = []
    open_count = 0
    for url in seminar_urls:
        sid = normalize_seminar_id(url)
        log.debug("Checking seminar: %s", url)
        html = fetch_seminar_page(url)
        if not html:
            continue
        data = parse_seminar_page(html, url)
        if not data or not data.get("register_url"):
            continue
        seminar_year = get_seminar_year(data)
        if seminar_year is not None and seminar_year != current_year:
            continue
        open_count += 1
        if sid in notified:
            continue
        to_notify.append((sid, url, data))

    return to_notify, open_count, len(seminar_urls)


async def do_check(
    bot: discord.Client,
    channel_id: int,
    ping: str,
    interval_minutes: float,
) -> None:
    """Run one seminar check using the running bot (send + status update)."""
    loop = asyncio.get_event_loop()
    to_notify, open_count, seminaries_on_list = await loop.run_in_executor(
        None,
        lambda: run_check_compute(interval_minutes),
    )

    channel = await bot.fetch_channel(channel_id)
    if channel is None:
        log.error("Channel %s not found", channel_id)
        return

    new_notifications = 0
    for sid, url, data in to_notify:
        register_url = data.get("register_url")
        if register_url:
            # Check if the register page actually loads and is open (not "niet beschikbaar" or "gesloten")
            is_available = await loop.run_in_executor(
                None,
                lambda u=register_url: check_register_page_available(u),
            )
            data = {**data, "register_available": is_available}
            use_ping = ping if is_available else ""
            if not is_available:
                log.info("Posting to Discord without ping (register page unavailable/closed): %s", data.get("title", url))
        else:
            use_ping = ping
        log.info("Sending notification: %s", data.get("title", url))
        if await _send_seminar_embed_async(channel, data, use_ping):
            mark_notified(sid, seminar_url=url, title=data.get("title"))
            new_notifications += 1
            log.info("Notified: %s", data.get("title", url))
        else:
            log.warning("Discord send failed for: %s", data.get("title", url))

    total = get_notified_count()
    log.info("Done. Notified %d new this run. Total %d.", new_notifications, total)

    last_check = datetime.now(timezone.utc)
    next_update_utc = last_check + timedelta(minutes=max(1, interval_minutes))
    status_embed = build_status_embed(
        seminaries_on_list=seminaries_on_list,
        open_for_registration=open_count,
        total_notified=total,
        new_this_run=new_notifications,
        last_check=last_check,
        next_update_utc=next_update_utc,
    )
    try:
        await _update_status_with_bot(bot, channel_id, status_embed)
    except Exception as e:
        log.warning("Status embed update failed: %s", e)

    # Update bot presence with current stats (fun status)
    await _set_bot_presence(bot, open_count, total, interval_minutes)


def _format_bot_activity(open_count: int, total_notified: int, interval_minutes: float) -> str:
    """Short, fun status line for the bot (max 128 chars for Discord)."""
    parts = []
    if open_count == 0:
        parts.append("no open seminars")
    elif open_count == 1:
        parts.append("1 open seminar")
    else:
        parts.append(f"{open_count} open seminars")
    parts.append(f"{total_notified} notified")
    if interval_minutes >= 1:
        parts.append(f"every {int(interval_minutes)}m")
    return " • ".join(parts)[:128]


async def _set_bot_presence(
    bot: discord.Client,
    open_count: int,
    total_notified: int,
    interval_minutes: float,
) -> None:
    """Set the bot's visible activity (status) with current stats."""
    try:
        name = _format_bot_activity(open_count, total_notified, interval_minutes)
        activity = discord.Activity(type=discord.ActivityType.watching, name=name)
        await bot.change_presence(activity=activity)
        log.debug("Presence set: %s", name)
    except Exception as e:
        log.debug("Could not set presence: %s", e)


class SeminarReminderBot(discord.Client):
    """Long-lived bot that runs the seminar check on a schedule."""

    def __init__(self, *, channel_id: int, ping: str, interval_minutes: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._channel_id = channel_id
        self._ping = ping
        self._interval_minutes = max(1.0, interval_minutes)

    async def setup_hook(self) -> None:
        """Called after login, before connection. Run first check and start loop."""
        init_db()
        log.info("Bot ready. Running first check, then every %.0f min.", self._interval_minutes)
        asyncio.create_task(self._check_loop())

    async def on_ready(self) -> None:
        """Set initial presence once the WebSocket is connected."""
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"PXL seminaries • check every {int(self._interval_minutes)}m",
            ),
        )

    async def _check_loop(self) -> None:
        """Run do_check immediately, then every interval_minutes."""
        while True:
            try:
                await do_check(self, self._channel_id, self._ping, self._interval_minutes)
            except Exception as e:
                log.exception("Check failed: %s", e)
            await asyncio.sleep(max(60, self._interval_minutes * 60))


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="PXL Seminar Reminder – Discord bot that checks seminars on a schedule.")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"), choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="Log level")
    args = parser.parse_args()
    setup_logging(args.log_level)

    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id_raw = os.environ.get("DISCORD_CHANNEL_ID")
    if not token:
        log.error("DISCORD_BOT_TOKEN is not set")
        sys.exit(1)
    if not channel_id_raw:
        log.error("DISCORD_CHANNEL_ID is not set")
        sys.exit(1)
    if not os.environ.get("DATABASE_URL"):
        log.error("DATABASE_URL is not set")
        sys.exit(1)

    ping = (os.environ.get("DISCORD_PING") or "@everyone").strip() or ""
    interval = float(os.environ.get("CHECK_INTERVAL", "60"))

    intents = discord.Intents.none()
    bot = SeminarReminderBot(
        channel_id=int(channel_id_raw),
        ping=ping,
        interval_minutes=interval,
        intents=intents,
    )
    bot.run(token)


if __name__ == "__main__":
    main()
