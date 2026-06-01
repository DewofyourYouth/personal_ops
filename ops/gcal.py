import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = ZoneInfo("Asia/Jerusalem")


class GCal:
    def __init__(self, credentials_file: Path | None = None, token_file: Path | None = None):
        base = Path(__file__).parent.parent
        self.credentials_file = credentials_file or base / "credentials.json"
        self.token_file = token_file or base / "token.json"
        self.service_account_file = base / "service_account.json"
        # Service accounts can't use "primary" — must be the calendar owner's email
        self.calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

    def get_today_events(self) -> list:
        now = datetime.now(TZ)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        result = self._service().events().list(
            calendarId=self.calendar_id,
            timeMin=now.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])

    def get_upcoming_events(self, within_minutes: int = 15) -> list:
        now = datetime.now(timezone.utc)
        result = self._service().events().list(
            calendarId=self.calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(minutes=within_minutes)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])

    def create_event(self, summary: str, start_dt: datetime,
                     duration_minutes: int = 60, description: str | None = None) -> dict:
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        body = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Jerusalem"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "Asia/Jerusalem"},
        }
        if description:
            body["description"] = description
        return self._service().events().insert(calendarId=self.calendar_id, body=body).execute()

    def format_events(self, events: list) -> str:
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

    def _service(self):
        if self.service_account_file.exists():
            from google.oauth2.service_account import Credentials as SACredentials
            creds = SACredentials.from_service_account_file(
                str(self.service_account_file), scopes=SCOPES
            )
        else:
            creds = None
            if self.token_file.exists():
                creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
                    creds = flow.run_local_server(port=0)
                self.token_file.write_text(creds.to_json())
        return build("calendar", "v3", credentials=creds)
