import io
import json
import os
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError
import requests
from local_env import load_local_env
from scraper import render_pdf_page_images


load_local_env(Path(__file__).parent / ".env")


SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/drive.readonly"
]

BASE_DIR = Path(__file__).parent

TOKEN_FILE = BASE_DIR / "token.json"
STATE_FILE = BASE_DIR / "classroom_seen.json"
FOOTER_ICON_FILE = BASE_DIR / "adv2.png"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
CLASSROOM_WEBHOOK_URL = os.environ.get("CLASSROOM_WEBHOOK_URL", "")
CLASSROOM_LOGO_URL = os.environ.get(
    "CLASSROOM_LOGO_URL",
    "https://ssl.gstatic.com/classroom/ic_product_classroom_32.png"
)
TEST_CLASSROOM_ANNOUNCEMENT_ID = os.environ.get(
    "TEST_CLASSROOM_ANNOUNCEMENT_ID",
    ""
).strip()
TEST_CLASSROOM_LATEST = os.environ.get(
    "TEST_CLASSROOM_LATEST",
    ""
).strip().lower() in {"1", "true", "yes", "on"}
CLASSROOM_COURSE_FILTER = os.environ.get(
    "CLASSROOM_COURSE_FILTER",
    ""
).strip()  


def get_credentials():
    creds = None

    if TOKEN_FILE.exists():
        try:
            token_data = json.loads(TOKEN_FILE.read_text())
            token_scopes = set(token_data.get("scopes", []))
        except Exception:
            token_scopes = set()

        if token_scopes.issuperset(SCOPES):
            creds = Credentials.from_authorized_user_file(
                TOKEN_FILE,
                SCOPES
            )

    has_required_scopes = creds and creds.has_scopes(SCOPES)

    if not creds or not creds.valid or not has_required_scopes:

        if creds and creds.expired and creds.refresh_token and has_required_scopes:
            creds.refresh(Request())

        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                BASE_DIR / "credentials.json",
                SCOPES
            )

            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())

    return creds


def load_seen():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            return set()

    return set()


def save_seen(seen):
    STATE_FILE.write_text(
        json.dumps(sorted(seen), indent=2)
    )


def get_posted_at():
    if ZoneInfo is not None:
        try:
            nepal_tz = ZoneInfo("Asia/Kathmandu")
            return datetime.now(nepal_tz).strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            pass

    return datetime.now().astimezone().strftime("%Y-%m-%d %I:%M %p")


def get_log_timestamp() -> str:
    return get_posted_at()


def get_announcement_preview(announcement):
    materials = announcement.get("materials", []) or []

    image_url = ""

    for material in materials:
        drive_file = material.get("driveFile", {}).get("driveFile", {})
        thumbnail_url = drive_file.get("thumbnailUrl", "").strip()

        if not image_url and thumbnail_url:
            image_url = thumbnail_url

    return image_url


def get_course_members(service, course_id):
    members = {}

    for collection in (service.courses().teachers(), service.courses().students()):
        request = collection.list(courseId=course_id, pageSize=100)

        while request:
            response = request.execute()
            for member in response.get("teachers", []) + response.get("students", []):
                user_id = member.get("userId", "")
                full_name = (
                    member.get("profile", {})
                    .get("name", {})
                    .get("fullName", "")
                    .strip()
                )
                if user_id and full_name:
                    members[user_id] = full_name

            request = collection.list_next(request, response)

    return members


def resolve_creator_name(creator_user_id, course_members):
    if not creator_user_id:
        return "Classroom user"

    return course_members.get(creator_user_id, "Classroom user")


def should_ping_everyone(course_name):
    normalized_name = " ".join(course_name.split()).casefold()
    return "electronics 2082 batch" in normalized_name


def should_monitor_course(course_name):
    if not CLASSROOM_COURSE_FILTER:
        return True
    
    normalized_course = " ".join(course_name.split()).casefold()
    filter_names = [
        " ".join(name.split()).casefold() 
        for name in CLASSROOM_COURSE_FILTER.split(",")
    ]
    return normalized_course in filter_names


def download_drive_file(drive_service, file_id):
    buffer = io.BytesIO()
    request = drive_service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    return buffer.getvalue()


def build_announcement_attachments(drive_service, announcement):
    attachments = []
    materials = announcement.get("materials", []) or []

    for material in materials:
        drive_file = material.get("driveFile", {}).get("driveFile", {})
        file_id = drive_file.get("id", "")

        if not file_id:
            continue

        try:
            file_info = drive_service.files().get(
                fileId=file_id,
                fields="id,name,mimeType"
            ).execute()
        except HttpError as exc:
            print(
                "Could not read Classroom attachment from Google Drive. "
                "Enable the Drive API for this Google Cloud project to post PDFs/images."
            )
            print(f"Drive API error: {exc}")
            continue

        mime_type = file_info.get("mimeType", "")
        file_name = file_info.get("name", "attachment")

        try:
            file_bytes = download_drive_file(drive_service, file_id)
        except Exception as exc:
            print(f"Failed to download Classroom attachment {file_name}: {exc}")
            continue

        if mime_type == "application/pdf":
            for page_bytes, page_name in render_pdf_page_images(file_bytes):
                attachments.append((page_name, page_bytes, "image/png"))
        elif mime_type.startswith("image/"):
            extension = Path(file_name).suffix or ".png"
            attachments.append((f"image{extension}", file_bytes, mime_type))

    return attachments


def send_embed_message(payload, footer_icon_bytes=None):
    webhook_url = CLASSROOM_WEBHOOK_URL or DISCORD_WEBHOOK_URL

    if footer_icon_bytes is None:
        return requests.post(
            webhook_url,
            json=payload,
            timeout=15
        )

    files = {
        "files[0]": ("adv2.png", footer_icon_bytes, "image/png")
    }

    return requests.post(
        webhook_url,
        data={"payload_json": json.dumps(payload)},
        files=files,
        timeout=30
    )


def send_attachment_message(attachments):
    webhook_url = CLASSROOM_WEBHOOK_URL or DISCORD_WEBHOOK_URL

    files = {
        f"files[{index}]": (filename, content, mime_type)
        for index, (filename, content, mime_type) in enumerate(attachments)
    }

    return requests.post(
        webhook_url,
        data={"payload_json": json.dumps({})},
        files=files,
        timeout=30
    )


def get_courses(service):
    courses = []

    request = service.courses().list(
        pageSize=100
    )

    while request:
        response = request.execute()

        courses.extend(
            response.get("courses", [])
        )

        request = service.courses().list_next(
            request,
            response
        )

    return courses


def get_announcements(service, course_id):
    announcements = []

    request = service.courses().announcements().list(
        courseId=course_id,
        pageSize=100,
        announcementStates=["PUBLISHED"]
    )

    while request:
        response = request.execute()

        announcements.extend(
            response.get("announcements", [])
        )

        request = service.courses().announcements().list_next(
            request,
            response
        )

    return announcements


def send_to_discord(course, announcement, course_members, drive_service):

    webhook_url = CLASSROOM_WEBHOOK_URL or DISCORD_WEBHOOK_URL

    if not webhook_url:
        print("CLASSROOM_WEBHOOK_URL is not configured.")
        return

    text = announcement.get(
        "text",
        "New Google Classroom announcement"
    )

    link = announcement.get(
        "alternateLink",
        ""
    )

    posted_at = get_posted_at()
    announcement_text = text.strip() or "New Google Classroom announcement"
    creator_name = resolve_creator_name(
        announcement.get("creatorUserId", ""),
        course_members
    )
    ping_everyone = should_ping_everyone(course.get("name", ""))
    attachments = build_announcement_attachments(drive_service, announcement)
    footer_icon_bytes = FOOTER_ICON_FILE.read_bytes() if FOOTER_ICON_FILE.exists() else None

    payload = {
        "embeds": [
            {
                "author": {
                    "name": "Google Classroom",
                    "icon_url": CLASSROOM_LOGO_URL,
                },
                "title": course.get("name", "Google Classroom"),
                "url": link,
                "description": f"📢 {announcement_text[:3800]}",
                "fields": [
                    {
                        "name": "👤 Posted by",
                        "value": creator_name,
                        "inline": True
                    },
                    {
                        "name": "🔗 Link",
                        "value": link or "Unavailable",
                        "inline": False
                    }
                ],
                "color": 0x5865F2,
                "footer": {
                    "text": f"• Google Classroom • Course: {course.get('name', 'Google Classroom')} • Posted on • {posted_at}",
                    "icon_url": "attachment://adv2.png" if footer_icon_bytes else CLASSROOM_LOGO_URL
                }
            }
        ]
    }

    if ping_everyone:
        payload["content"] = "@everyone"
        payload["allowed_mentions"] = {"parse": ["everyone"]}

    response = send_embed_message(payload, footer_icon_bytes)

    if response.status_code in (200, 204) and attachments:
        response = send_attachment_message(attachments)

    if response.status_code not in (200, 204):
        print(
            f"Discord error {response.status_code}: "
            f"{response.text}"
        )

    else:
        print(
            f"New record found and sent time: {get_log_timestamp()} | "
            f"Classroom announcement {announcement['id']} from {course.get('name')}"
        )


def check_classroom():

    credentials = get_credentials()

    service = build(
        "classroom",
        "v1",
        credentials=credentials
    )
    drive_service = build(
        "drive",
        "v3",
        credentials=credentials
    )

    seen = load_seen()

    courses = get_courses(service)

    if not courses:
        print("No Google Classroom courses found.")
        return

    print(
        f"Found {len(courses)} Classroom course(s)."
    )
    
    for course in courses:
        print(f"  - {course.get('name', 'Unknown Course')}")

    all_announcements = []

    for course in courses:

        course_id = course["id"]
        course_name = course.get("name", "")

        if not should_monitor_course(course_name):
            print(
                f"Skipping: {course_name} (not in filter)"
            )
            continue

        course_members = get_course_members(service, course_id)

        print(
            f"Checking: {course_name}"
        )

        announcements = get_announcements(
            service,
            course_id
        )

        for announcement in announcements:

            announcement_id = announcement["id"]

            all_announcements.append(
                (
                    course,
                    announcement,
                    course_members
                )
            )

    if TEST_CLASSROOM_LATEST:
        if not all_announcements:
            print("No Classroom announcements were found to test.")
            return

        course, announcement, course_members = all_announcements[0]

        print(
            f"Test mode: sending latest Classroom announcement {announcement['id']} from {course.get('name')}"
        )

        send_to_discord(course, announcement, course_members, drive_service)

        return

    if TEST_CLASSROOM_ANNOUNCEMENT_ID:
        test_target = next(
            (
                (course, announcement, course_members)
                for course, announcement, course_members in all_announcements
                if announcement["id"] == TEST_CLASSROOM_ANNOUNCEMENT_ID
            ),
            None
        )

        if not test_target:
            print(
                f"TEST_CLASSROOM_ANNOUNCEMENT_ID={TEST_CLASSROOM_ANNOUNCEMENT_ID} was not found."
            )
            return

        course, announcement, course_members = test_target

        print(
            f"Test mode: sending Classroom announcement {announcement['id']} from {course.get('name')}"
        )

        send_to_discord(course, announcement, course_members, drive_service)

        return

    if not seen:

        seen = {
            announcement["id"]
            for _, announcement, _ in all_announcements
        }

        save_seen(seen)

        print(
            f"No new record found time: {get_log_timestamp()} | "
            f"first Classroom run baseline recorded {len(seen)} announcements."
        )

        return

    new_count = 0

    for course, announcement, course_members in all_announcements:

        announcement_id = announcement["id"]

        if announcement_id in seen:
            continue

        print(
            f"NEW Classroom announcement: "
            f"{course.get('name')}"
        )

        send_to_discord(course, announcement, course_members, drive_service)

        seen.add(announcement_id)

        new_count += 1

    save_seen(seen)

    if new_count == 0:
        print(
            f"No new record found time: {get_log_timestamp()} | "
            f"0 new Classroom announcement(s)."
        )
    else:
        print(
            f"New record found and sent time: {get_log_timestamp()} | "
            f"{new_count} new Classroom announcement(s)."
        )


if __name__ == "__main__":
    check_classroom()