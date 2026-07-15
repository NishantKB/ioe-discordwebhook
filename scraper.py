import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

IOE_URL = os.environ.get("IOE_URL", "https://exam.ioe.edu.np/")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

FILTER_KEYWORD = os.environ.get("FILTER_KEYWORD", "result")

STATE_FILE = Path(__file__).parent / "seen_notices.json"

HEADERS = {

    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def load_seen_ids() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()


def save_seen_ids(ids: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))


def fetch_notices() -> list[dict]:
    """Scrape the IOE homepage for notice links.

    Individual notices live at /Notice/Index/{id}. We grab every link on
    the homepage that matches that pattern, along with its visible text
    as the title.
    """
    resp = requests.get(IOE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    notices = []
    seen_ids_this_page = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"Notice/Index/(\d+)", href)
        if not match:
            continue
        notice_id = match.group(1)
        if notice_id in seen_ids_this_page:
            continue
        seen_ids_this_page.add(notice_id)

        title = a.get_text(strip=True)
        if not title:
            # Sometimes the visible text is on a sibling/parent element
            # instead of the <a> itself - fall back to the row text.
            parent = a.find_parent(["tr", "li", "div"])
            title = parent.get_text(strip=True) if parent else f"Notice {notice_id}"

        full_url = href if href.startswith("http") else f"https://exam.ioe.edu.np{href if href.startswith('/') else '/' + href}"

        notices.append({"id": notice_id, "title": title, "url": full_url})

    return notices


def notify_discord(notice: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set - skipping Discord post. Notice:", notice)
        return

    payload = {
        "content": f"📢 **New IOE notice published!**\n**{notice['title']}**\n{notice['url']}"
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if resp.status_code not in (200, 204):
        print(f"Discord webhook failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    else:
        print(f"Notified Discord about notice {notice['id']}: {notice['title']}")


def main() -> None:
    seen_ids = load_seen_ids()
    notices = fetch_notices()

    if not notices:
        print("No notices found on the page - the site structure may have changed. "
              "Open the homepage in a browser and check scraper.py's selector logic.")
        return

    first_run = len(seen_ids) == 0
    new_notices = [n for n in notices if n["id"] not in seen_ids]

    if first_run:
        print(f"First run: recording {len(notices)} existing notices as baseline, no notifications sent.")
        save_seen_ids({n["id"] for n in notices})
        return

    keyword = FILTER_KEYWORD.strip().lower()
    for notice in new_notices:
        seen_ids.add(notice["id"])
        if keyword and keyword not in notice["title"].lower():
            print(f"Skipping (doesn't match keyword '{keyword}'): {notice['title']}")
            continue
        notify_discord(notice)

    save_seen_ids(seen_ids)
    print(f"Done. {len(new_notices)} new notice(s) found this run.")


if __name__ == "__main__":
    main()
