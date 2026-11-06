"""Groq chat-completions client (OpenAI-compatible REST API, called with httpx).

- Explicit timeouts; one retry on 429 / 5xx / timeout (completions have no
  side effects, so a retry is safe).
- Token usage and an estimated cost are returned with every call so the
  action log can show cost per run.
- Health is recorded on the "llm" connection row, so a bad key or an outage
  shows in the dashboard.
"""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Connection

URL = "https://api.groq.com/openai/v1/chat/completions"
# USD per million tokens (input, output), from console.groq.com/docs/models, Nov 2026.
PRICES = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}

# Tests replace this with httpx.MockTransport. The running app uses the real API.
HTTP_TRANSPORT: httpx.BaseTransport | None = None


class LLMError(Exception):
    """The model could not produce a usable answer. `reason` is shown to operators."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    extra: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        inp, out = PRICES.get(self.model, (0.0, 0.0))
        return round((self.prompt_tokens * inp + self.completion_tokens * out) / 1_000_000, 6)

    def usage(self) -> dict:
        return {"model": self.model, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cost_usd": self.cost_usd,
                "latency_ms": self.latency_ms, "attempts": self.attempts}


def _record_health(session: Session | None, status: str, error: str = "") -> None:
    if session is None:
        return
    row = session.get(Connection, "llm") or Connection(provider="llm", credentials={}, state={})
    if row.status != status or row.last_error != error or row.last_checked_at is None:
        row.status, row.last_error = status, error
        row.last_checked_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()


def chat(
    messages: list[dict],
    *,
    model: str,
    session: Session | None = None,
    json_schema: dict | None = None,
    temperature: float = 0.0,
    max_tokens: int = 1200,
    max_attempts: int = 2,
    max_wait: float = 5.0,
) -> LLMResult:
    """max_attempts / max_wait: background work (the agents) can afford to wait out a rate
    limit; a live reply to a lead should not hang, so the defaults stay short."""
    if not settings.groq_api_key:
        _record_health(session, "error", "GROQ_API_KEY is not set")
        raise LLMError("The language model is not configured (GROQ_API_KEY missing)")

    body: dict = {"model": model, "messages": messages, "temperature": temperature,
                  "max_completion_tokens": max_tokens}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
    if json_schema:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": json_schema["name"], "strict": True,
                                                   "schema": json_schema["schema"]}}

    started = time.monotonic()
    last_reason = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0), transport=HTTP_TRANSPORT) as http:
                resp = http.post(URL, json=body, headers={"Authorization": f"Bearer {settings.groq_api_key}"})
        except httpx.TimeoutException:
            last_reason = "the model did not answer in time"
        except httpx.HTTPError as exc:
            last_reason = f"could not reach Groq ({exc.__class__.__name__})"
        else:
            if resp.status_code == 200:
                data = resp.json()
                usage = data.get("usage") or {}
                content = (data["choices"][0]["message"].get("content") or "").strip()
                _record_health(session, "ok")
                return LLMResult(content=content, model=data.get("model", model),
                                 prompt_tokens=int(usage.get("prompt_tokens", 0)),
                                 completion_tokens=int(usage.get("completion_tokens", 0)),
                                 latency_ms=int((time.monotonic() - started) * 1000), attempts=attempt)
            if resp.status_code in (401, 403):
                _record_health(session, "error", "Groq rejected the API key. Check GROQ_API_KEY.")
                raise LLMError("Groq rejected the API key")
            if resp.status_code == 404 or (resp.status_code == 400 and "model" in resp.text.lower()
                                           and "decommission" in resp.text.lower()):
                _record_health(session, "error", f"Model {model} is not available on Groq. Set GROQ_MODEL.")
                raise LLMError(f"model {model} is not available")
            if resp.status_code == 400:
                raise LLMError(f"Groq refused the request: {_short(resp)}")
            if resp.status_code == 413:
                _record_health(session, "error", "A request is larger than Groq's per-minute token limit.")
                raise LLMError("the request is larger than Groq's per-minute token limit for this model")
            last_reason = ("Groq is rate limiting requests (per-minute token limit)" if resp.status_code == 429
                           else f"Groq error {resp.status_code}")
            if attempt < max_attempts:
                time.sleep(min(_retry_after(resp), max_wait))
                continue
        if attempt < max_attempts:
            time.sleep(1.0)
    _record_health(session, "error", f"Last call failed: {last_reason}")
    raise LLMError(last_reason)


def _retry_after(resp: httpx.Response) -> float:
    """Seconds Groq asks us to wait: the retry-after header, or "try again in 1m2.5s" in the message."""
    header = resp.headers.get("retry-after", "")
    try:
        return float(header) + 0.5
    except ValueError:
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", resp.text)
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2)) + 0.5
    return 2.0


def _short(resp: httpx.Response) -> str:
    try:
        return json.dumps(resp.json().get("error", {}).get("message", ""))[:200]
    except ValueError:
        return resp.text[:200]
