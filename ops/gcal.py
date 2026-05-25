from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_FILE = Path(__file__).parent.parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent.parent / "token.json"
TZ = ZoneInfo("Asia/Jerusalem")


def _service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def get_today_events():
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    result = _service().events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def get_upcoming_events(within_minutes=15):
    now = datetime.now(timezone.utc)
    result = _service().events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=(now + timedelta(minutes=within_minutes)).isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def create_event(summary, start_dt, duration_minutes=60, description=None):
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Jerusalem"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Jerusalem"},
    }
    if description:
        body["description"] = description
    return _service().events().insert(calendarId="primary", body=body).execute()


def format_events(events):
    if not events:
        return "No events today."
    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        summary = e.get("summary", "(no title)")
        if "T" in start:
            t = datetime.fromisoformat(start).astimezone(TZ).strftime("%H:%M")
            lines.append(f"• {t} — {summary}")
        else:
            lines.append(f"• All day — {summary}")
    return "\n".join(lines)
