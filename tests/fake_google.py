"""A small in-memory stand-in for Google's token and Gmail endpoints, used
only by tests (httpx.MockTransport). It is not used by the running app."""
import base64
import json
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

    def deliver(self, msg: dict):
        self.messages[msg["id"]] = msg
        self.history.append(msg["id"])
        self.history_id += 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, url = request.url.path, str(request.url)
        self.calls.append(f"{request.method} {path}")
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
