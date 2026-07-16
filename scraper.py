import json
import io
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import fitz
from bs4 import BeautifulSoup
from PIL import Image

IOE_URL = os.environ.get("IOE_URL", "https://exam.ioe.tu.edu.np/notices")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

FILTER_KEYWORD = os.environ.get("FILTER_KEYWORD", "Result")
TEST_NOTICE_ID = os.environ.get("TEST_NOTICE_ID", "").strip()

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
        match = re.search(r"(?:Notice/Index|notices)/(\d+)", href, re.IGNORECASE)
        if not match:
            continue
        notice_id = match.group(1)
        if notice_id in seen_ids_this_page:
            continue
        seen_ids_this_page.add(notice_id)

        title = a.get_text(strip=True)
        if not title:
            parent = a.find_parent(["tr", "li", "div"])
            title = parent.get_text(strip=True) if parent else f"Notice {notice_id}"

        full_url = urljoin(IOE_URL, href)

        notices.append({"id": notice_id, "title": title, "url": full_url})

    return notices


def fetch_notice_image_url(notice_url: str) -> str:
    return fetch_notice_image_url_with_title(notice_url, "")


def fetch_notice_image_url_with_title(notice_url: str, notice_title: str) -> str:
    try:
        resp = requests.get(notice_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    metadata_selectors = [
        ('meta[property="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="twitter:image"]', "content"),
        ('link[rel="image_src"]', "href"),
    ]
    for selector, attribute in metadata_selectors:
        tag = soup.select_one(selector)
        if tag and tag.get(attribute):
            return urljoin(notice_url, tag[attribute])

    normalized_title = re.sub(r"\s+", " ", notice_title.lower()).strip()
    title_tokens = [token for token in re.findall(r"[a-z0-9]+", normalized_title) if len(token) > 2]

    if normalized_title:
        body_candidates: list[tuple[int, int, str]] = []
        for container in soup.find_all(["main", "article", "section", "div", "p"]):
            container_text = re.sub(r"\s+", " ", container.get_text(" ", strip=True).lower()).strip()
            if normalized_title not in container_text:
                continue
            container_images = []
            for image_tag in container.select("img"):
                source = image_tag.get("src") or image_tag.get("data-src")
                if not source:
                    continue
                absolute_source = urljoin(notice_url, source)
                if "assets/logo" in absolute_source or "calendar.png" in absolute_source:
                    continue
                container_images.append(absolute_source)

            if not container_images:
                continue

            body_candidates.append((len(container_text), len(container_images), container_images[0]))

        if body_candidates:
            body_candidates.sort(key=lambda item: (item[0], item[1]))
            return body_candidates[0][2]

    candidates: list[tuple[int, int, str]] = []
    seen_sources: set[str] = set()
    for image_tag in soup.select("article img, main img, .content img, .post img, img"):
        source = image_tag.get("src") or image_tag.get("data-src")
        if not source:
            continue
        absolute_source = urljoin(notice_url, source)
        if "assets/logo" in absolute_source or "calendar.png" in absolute_source:
            continue
        if absolute_source in seen_sources:
            continue
        seen_sources.add(absolute_source)
        text_context = " ".join(
            ancestor.get_text(" ", strip=True)
            for ancestor in [image_tag.parent, image_tag.parent.parent if image_tag.parent else None]
            if ancestor is not None
        ).lower()
        exact_title_match = 1 if normalized_title and normalized_title in text_context else 0
        token_hits = sum(1 for token in title_tokens if token in text_context)
        candidates.append((exact_title_match, token_hits, absolute_source))

    best_source = ""
    best_score = (-1, -1)
    best_area = 0
    for exact_title_match, token_hits, candidate in candidates:
        try:
            image_resp = requests.get(candidate, headers=HEADERS, timeout=30)
            image_resp.raise_for_status()
            image = Image.open(io.BytesIO(image_resp.content))
            area = image.width * image.height
            score = (exact_title_match, token_hits)
            if score > best_score or (score == best_score and area > best_area):
                best_score = score
                best_area = area
                best_source = candidate
        except Exception:
            continue

    if best_source:
        return best_source

    return ""


def fetch_notice_ck_table_image_url(notice_url: str) -> str:
    try:
        resp = requests.get(notice_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    ck_table = soup.select_one("div.ck-table")
    if not ck_table:
        return ""

    for image_tag in ck_table.select("img"):
        source = image_tag.get("src") or image_tag.get("data-src")
        if source:
            return urljoin(notice_url, source)

    return ""


def fetch_notice_ck_table_assets(notice_url: str) -> tuple[str, str]:
    try:
        resp = requests.get(notice_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return "", ""

    soup = BeautifulSoup(resp.text, "html.parser")
    ck_table = soup.select_one("div.ck-table")
    if not ck_table:
        return "", ""

    pdf_url = ""
    image_url = ""

    for link_tag in ck_table.select("a[href]"):
        href = link_tag.get("href", "")
        if href and re.search(r"\.pdf(\?|$)", href, re.IGNORECASE):
            pdf_url = urljoin(notice_url, href)
            break

    image_tag = ck_table.select_one("img")
    if image_tag:
        source = image_tag.get("src") or image_tag.get("data-src")
        if source:
            image_url = urljoin(notice_url, source)

    return pdf_url, image_url


def fetch_notice_file_url(notice_url: str) -> tuple[str, str]:
    try:
        resp = requests.get(notice_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return "", ""

    soup = BeautifulSoup(resp.text, "html.parser")

    for link_tag in soup.select("a[href]"):
        href = link_tag.get("href", "")
        if not href:
            continue
        if re.search(r"\.pdf(\?|$)", href, re.IGNORECASE):
            file_url = urljoin(notice_url, href)
            filename = Path(file_url.split("?")[0]).name or "notice-file"
            return file_url, filename

    return "", ""


def render_pdf_page_images(pdf_bytes: bytes) -> list[tuple[bytes, str]]:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []

    try:
        if doc.page_count == 0:
            return []

        page_images = []
        for page_number in range(doc.page_count):
            page = doc.load_page(page_number)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
            image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)

            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            page_images.append((output.getvalue(), f"page-{page_number + 1}.png"))

        return page_images
    finally:
        doc.close()


def notify_discord(notice: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set - skipping Discord post. Notice:", notice)
        return

    pdf_url, notice_image_url = fetch_notice_ck_table_assets(notice["url"])
    payload = {
        "content": f"📢 **New IOE notice published!**\n**{notice['title']}**\n{notice['url']}"
    }
    if pdf_url:
        try:
            pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
            pdf_resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"Failed to download notice PDF attachment: {exc}", file=sys.stderr)
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        else:
            mime_type = pdf_resp.headers.get("Content-Type", "application/octet-stream")
            pdf_name = Path(pdf_url.split("?")[0]).name or "notice.pdf"
            files = {"files[0]": (pdf_name, pdf_resp.content, mime_type)}

            page_images = render_pdf_page_images(pdf_resp.content)
            for index, (page_bytes, page_name) in enumerate(page_images, start=1):
                files[f"files[{index}]"] = (page_name, page_bytes, "image/png")

            resp = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"payload_json": json.dumps(payload)},
                files=files,
                timeout=30,
            )
    elif notice_image_url:
        try:
            image_resp = requests.get(notice_image_url, headers=HEADERS, timeout=30)
            image_resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"Failed to download notice image attachment: {exc}", file=sys.stderr)
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        else:
            mime_type = image_resp.headers.get("Content-Type", "application/octet-stream")
            image_name = Path(notice_image_url.split("?")[0]).name or "notice-image"
            files = {"files[0]": (image_name, image_resp.content, mime_type)}
            resp = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"payload_json": json.dumps(payload)},
                files=files,
                timeout=30,
            )
    else:
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

    if TEST_NOTICE_ID:
        test_notice = next((n for n in notices if n["id"] == TEST_NOTICE_ID), None)
        if not test_notice:
            print(f"TEST_NOTICE_ID={TEST_NOTICE_ID} was not found on the page.")
            return
        print(f"Test mode: sending notice {test_notice['id']} regardless of baseline state.")
        notify_discord(test_notice)
        return

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
