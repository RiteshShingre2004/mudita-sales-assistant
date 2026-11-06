"""A small in-memory stand-in for Google's token and Gmail endpoints, used
only by tests (httpx.MockTransport). It is not used by the running app."""
import base64
import json
from datetime import datetime
from urllib.parse import parse_qs

import httpx


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmail_message(mid, sender, body, *, subject="Inquiry", labels=("INBOX", "UNREAD"), headers=None,
                  ts=1_700_000_000_000, to="desk@lumen-test.example", thread=None):
    hdrs = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
            {"name": "Message-ID", "value": f"<{mid}@mail.example>"}]
    for k, v in (headers or {}).items():
        hdrs.append({"name": k, "value": v})
    return {"id": mid, "threadId": thread or f"t-{mid}", "labelIds": list(labels), "internalDate": str(ts),
            "payload": {"mimeType": "multipart/alternative", "headers": hdrs,
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64(body)}}]}}


class FakeGoogle:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.history: list[str] = []      # message ids in arrival order
        self.history_id = 100
        self.sent: list[dict] = []
        self.refresh_fails = False
        self.history_404 = False
        self.fail_get_for: set[str] = set()
        self.calls: list[str] = []
        # calendar
        self.busy: list[tuple[datetime, datetime]] = []
        self.events: dict[str, dict] = {}
        self.inserts: list[str] = []
        self.insert_timeouts = 0          # next N inserts time out
        self.timeout_after_create = False  # the timed-out insert still created the event
        self.busy_hook = None              # callable run on each freeBusy (to simulate a slot being taken)

    def deliver(self, msg: dict):
        self.messages[msg["id"]] = msg
        self.history.append(msg["id"])
        self.history_id += 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, url = request.url.path, str(request.url)
        self.calls.append(f"{request.method} {path}")
        if path.startswith("/calendar/v3/"):
            return self._calendar(request, path)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"] and self.refresh_fails:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={
                "access_token": "at-new", "expires_in": 3600, "refresh_token": "rt-1",
                "scope": " ".join([
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/calendar.events",
                    "https://www.googleapis.com/auth/calendar.freebusy"])})
        if path.endswith("/users/me/profile"):
            return httpx.Response(200, json={"emailAddress": "desk@lumen-test.example", "historyId": str(self.history_id)})
        if path.endswith("/users/me/history"):
            if self.history_404:
                return httpx.Response(404, json={"error": {"message": "Requested entity was not found."}})
            start = int(request.url.params["startHistoryId"])
            first_index = max(0, start - (self.history_id - len(self.history)))
            added = [{"messagesAdded": [{"message": {"id": mid}}]} for mid in self.history[first_index:]]
            return httpx.Response(200, json={"history": added, "historyId": str(self.history_id)})
        if path.endswith("/users/me/messages") and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": m} for m in self.history[-25:]]})
        if "/users/me/messages/" in path and request.method == "GET":
            mid = path.rsplit("/", 1)[-1]
            if mid in self.fail_get_for:
                return httpx.Response(500, json={"error": {"message": "backend error"}})
            fmt = request.url.params.get("format", "full")
            self.calls.append(f"get {mid} {fmt}")
            msg = self.messages[mid]
            if fmt == "metadata":  # headers only, no body
                payload = {"headers": msg["payload"]["headers"]}
                return httpx.Response(200, json={**{k: v for k, v in msg.items() if k != "payload"}, "payload": payload})
            return httpx.Response(200, json=msg)
        if path.endswith("/users/me/messages/send"):
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": body.get("threadId")})
        return httpx.Response(404, json={"error": f"unhandled {path}"})

    def _calendar(self, request: httpx.Request, path: str) -> httpx.Response:
        def parse(ts):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))

        if path.endswith("/freeBusy"):
            if self.busy_hook:
                self.busy_hook(self)
            body = json.loads(request.content)
            lo, hi = parse(body["timeMin"]), parse(body["timeMax"])
            spans = list(self.busy) + [(parse(e["start"]["dateTime"]), parse(e["end"]["dateTime"]))
                                       for e in self.events.values()]
            busy = [{"start": a.isoformat(), "end": b.isoformat()} for a, b in spans if a < hi and lo < b]
            return httpx.Response(200, json={"calendars": {"primary": {"busy": busy}}})
        if path.endswith("/events") and request.method == "POST":
            body = json.loads(request.content)
            self.inserts.append(body["id"])
            if self.insert_timeouts:
                self.insert_timeouts -= 1
                if self.timeout_after_create:
                    self.events[body["id"]] = {**body, "htmlLink": f"https://calendar.example/{body['id']}"}
                raise httpx.ReadTimeout("calendar timed out")
            if body["id"] in self.events:
                return httpx.Response(409, json={"error": {"code": 409, "message": "The requested identifier already exists."}})
            self.events[body["id"]] = {**body, "htmlLink": f"https://calendar.example/{body['id']}"}
            return httpx.Response(200, json=self.events[body["id"]])
        if "/events/" in path and request.method == "GET":
            eid = path.rsplit("/", 1)[-1]
            if eid in self.events:
                return httpx.Response(200, json=self.events[eid])
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})
        return httpx.Response(404, json={"error": f"unhandled calendar {path}"})
