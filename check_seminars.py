#!/usr/bin/env python3
"""
PXL Seminar reminder: checks seminar pages periodically and sends a Discord
webhook when the "Inschrijven" (register) link is no longer '#' (registration open).
Only one notification per seminar is sent.
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://pxl-digital.pxl.be"
SEMINARIES_LIST_URL = f"{BASE_URL}/i-talent/seminaries-2tin-25-26"
STATE_FILE = Path(__file__).resolve().parent / "notified_seminars.json"
USER_AGENT = "PXL-Seminar-Reminder/1.0"


def load_notified() -> set[str]:
    """Load set of seminar IDs we have already notified."""
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("notified", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_notified(notified: set[str]) -> None:
    """Persist notified seminar IDs."""
    STATE_FILE.write_text(
        json.dumps({"notified": sorted(notified)}, indent=2),
        encoding="utf-8",
    )


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
    except requests.RequestException:
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


def send_discord_webhook(webhook_url: str, seminar: dict, ping: str = "@everyone") -> bool:
    """Send one Discord webhook message with embed. Returns True on success."""
    embed = build_discord_embed(seminar)
    payload = {"embeds": [embed]}
    if ping and ping.strip():
        payload["content"] = ping.strip()
    try:
        r = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code in (200, 204):
            return True
        print(f"Discord webhook error: {r.status_code} {r.text}", file=sys.stderr)
        return False
    except requests.RequestException as e:
        print(f"Discord webhook request failed: {e}", file=sys.stderr)
        return False


def run_check(webhook_url: str | None = None, ping: str | None = None) -> None:
    """Fetch seminar list, check each detail page for open registration, notify once per seminar."""
    webhook_url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("Set DISCORD_WEBHOOK_URL or pass webhook_url to run_check().", file=sys.stderr)
        sys.exit(1)
    ping = (ping if ping is not None else os.environ.get("DISCORD_PING", "@everyone")) or ""

    notified = load_notified()

    try:
        r = requests.get(
            SEMINARIES_LIST_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Failed to fetch seminar list: {e}", file=sys.stderr)
        sys.exit(1)

    seminar_urls = get_seminar_links_from_list_page(r.text)
    if not seminar_urls:
        print("No seminar links found on list page.")
        return

    new_notifications = 0
    for url in seminar_urls:
        sid = normalize_seminar_id(url)
        if sid in notified:
            continue
        html = fetch_seminar_page(url)
        if not html:
            continue
        data = parse_seminar_page(html, url)
        if not data:
            continue
        if not data.get("register_url"):
            continue
        # Only notify for seminars in the current year (skip old events from previous years)
        seminar_year = get_seminar_year(data)
        if seminar_year is not None and seminar_year != date.today().year:
            continue
        if send_discord_webhook(webhook_url, data, ping):
            notified.add(sid)
            new_notifications += 1
            print(f"Notified: {data.get('title', url)}")

    if new_notifications:
        save_notified(notified)
    print(f"Done. Notified {new_notifications} new seminar(s).")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Check PXL seminars and notify Discord when registration opens.")
    parser.add_argument("--webhook", "-w", default=os.environ.get("DISCORD_WEBHOOK_URL"), help="Discord webhook URL (or set DISCORD_WEBHOOK_URL)")
    parser.add_argument("--ping", "-p", default=os.environ.get("DISCORD_PING", "@everyone"), help="Mention to ping (e.g. @everyone, @here, or <@&role_id>). Default: @everyone. Set empty to disable.")
    parser.add_argument("--loop", "-l", action="store_true", help="Run indefinitely, re-check every N minutes")
    parser.add_argument("--interval", "-i", type=float, default=60, help="Minutes between checks when using --loop (default: 60)")
    args = parser.parse_args()
    if not args.webhook:
        print("Error: set DISCORD_WEBHOOK_URL or pass --webhook URL", file=sys.stderr)
        sys.exit(1)
    if args.loop:
        import time
        while True:
            run_check(args.webhook, ping=args.ping)
            time.sleep(max(60, args.interval * 60))
    else:
        run_check(args.webhook, ping=args.ping)


if __name__ == "__main__":
    main()
