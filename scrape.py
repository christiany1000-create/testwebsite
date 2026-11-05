#!/usr/bin/env python3
"""
Disc to Digital scraper.

Each run:
  1. Checks every enabled source in sources.json. Failed fetches are retried.
  2. Collects game titles from the sources that returned usable data.
  3. Records the calendar day each game was seen on.
  4. Moves a game from "pending" to "confirmed" only after it has been seen on
     CONFIRMATIONS_REQUIRED different days, so one bad page can't add a game.
  5. Writes docs/data.json, which the website reads.

Run it with:  python scrape.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
SOURCES_FILE = HERE / "sources.json"
STATE_FILE = HERE / "data" / "state.json"
SITE_DATA_FILE = HERE.parent / "docs" / "data.json"

# A game must be seen on this many different days before it is confirmed.
CONFIRMATIONS_REQUIRED = int(os.environ.get("CONFIRMATIONS_REQUIRED", "3"))
FETCH_ATTEMPTS = 3
REQUEST_TIMEOUT = 20  # seconds
USER_AGENT = "DiscToDigitalTracker/1.0 (personal project)"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def normalize(title: str) -> str:
    """Make differently formatted titles comparable.

    'Wolfenstein® II: The New Colossus' -> 'wolfenstein ii the new colossus'
    """
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def allowed_by_robots(url: str) -> bool:
    """Respect robots.txt. If it can't be read at all, assume access is allowed."""
    parts = urlparse(url)
    rp = robotparser.RobotFileParser()
    rp.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
    try:
        rp.read()
    except Exception:
        return True
    return rp.can_fetch(USER_AGENT, url)


def extract_titles(html: str, selector: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    titles = set()
    for node in soup.select(selector):
        text = " ".join(node.get_text(" ", strip=True).split())
        if 2 <= len(text) <= 120:
            titles.add(text)
    return sorted(titles)


def check_source(source: dict) -> tuple[list[str], dict]:
    """Fetch one source, retrying on failure. Returns (titles, status)."""
    status = {
        "name": source["name"],
        "url": source["url"],
        "ok": False,
        "titles": 0,
        "error": None,
        "checked_at": utc_now().isoformat(),
    }

    if not allowed_by_robots(source["url"]):
        status["error"] = "Blocked by robots.txt"
        return [], status

    min_titles = source.get("min_titles", 20)
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            response = session.get(source["url"], timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            titles = extract_titles(response.text, source["selector"])
            # A redesigned page or a wrong selector would otherwise look like "no games".
            if len(titles) < min_titles:
                raise ValueError(
                    f"found {len(titles)} titles, expected at least {min_titles}. Check the selector."
                )
            status.update(ok=True, titles=len(titles), error=None)
            return titles, status
        except Exception as exc:  # network errors, HTTP errors, bad selectors
            status["error"] = f"Attempt {attempt} failed: {exc}"
            print(f"  {status['error']}")
            if attempt < FETCH_ATTEMPTS:
                time.sleep(5 * attempt)

    return [], status


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"games": {}}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def build_site_data(state: dict, statuses: list[dict], now: dt.datetime) -> dict:
    confirmed, pending = [], []

    for game in state["games"].values():
        days = game["seen_days"]  # sorted list of YYYY-MM-DD strings
        item = {
            "title": game["title"],
            "checks": len(days),
            "first_seen": days[0],
            "last_seen": days[-1],
            "sources": game["sources"],
        }
        if len(days) >= CONFIRMATIONS_REQUIRED:
            # The day the game reached the required number of separate checks.
            item["confirmed_on"] = days[CONFIRMATIONS_REQUIRED - 1]
            confirmed.append(item)
        else:
            pending.append(item)

    def by_title(game: dict) -> str:
        return game["title"].lower()

    return {
        "updated_at": now.isoformat(),
        "required_checks": CONFIRMATIONS_REQUIRED,
        "sources": statuses,
        "confirmed": sorted(confirmed, key=by_title),
        "pending": sorted(pending, key=by_title),
    }


def main() -> None:
    sources = [
        s for s in json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if s.get("enabled", True)
    ]
    state = load_state()
    now = utc_now()
    today = now.date().isoformat()

    statuses = []
    found: dict[str, dict] = {}  # normalized title -> {"title": ..., "sources": set()}

    for source in sources:
        print(f"Checking {source['name']}...")
        titles, status = check_source(source)
        statuses.append(status)
        for title in titles:
            key = normalize(title)
            if key:
                entry = found.setdefault(key, {"title": title, "sources": set()})
                entry["sources"].add(source["name"])

    if any(s["ok"] for s in statuses):
        for key, entry in found.items():
            game = state["games"].setdefault(
                key,
                {
                    "title": entry["title"],
                    "first_seen": now.isoformat(),
                    "seen_days": [],
                    "sources": [],
                },
            )
            if today not in game["seen_days"]:
                game["seen_days"].append(today)
                game["seen_days"].sort()
            game["last_seen"] = now.isoformat()
            game["sources"] = sorted(set(game["sources"]) | entry["sources"])
    else:
        # Every source failed, so this run tells us nothing. Keep the previous results.
        print("No source returned usable data this run. Keeping previous results.")

    save_json(STATE_FILE, state)
    site_data = build_site_data(state, statuses, now)
    save_json(SITE_DATA_FILE, site_data)
    print(
        f"Done: {len(site_data['confirmed'])} confirmed, "
        f"{len(site_data['pending'])} pending."
    )


if __name__ == "__main__":
    main()
