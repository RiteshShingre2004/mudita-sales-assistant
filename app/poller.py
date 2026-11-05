"""Background inbox poller (runs inside the web process).

A lock makes sure a scheduled poll and a "Check inbox now" click never run
at the same time. Errors are recorded on the connection (shown in the UI)
and logged once per change, not on every tick.
"""
import asyncio
import logging
import os
import threading

from app.db import SessionLocal
from app.google_oauth import GoogleAPIError, GoogleNotConnected, get_row, set_status

log = logging.getLogger("poller")
POLL_SECONDS = int(os.environ.get("GMAIL_POLL_SECONDS", "20"))
_lock = threading.Lock()


def poll_gmail_now(actor: str = "poller") -> dict:
    from app.channels.gmail import poll_once

    if not _lock.acquire(timeout=30):
        return {"busy": True}
    try:
        with SessionLocal() as s:
            row = get_row(s)
            s.commit()
            if not (row.credentials or {}).get("refresh_token"):
                return {"connected": False}
            try:
                return {"connected": True, **poll_once(s, actor=actor)}
            except GoogleNotConnected as exc:
                return {"connected": False, "error": str(exc)}
            except GoogleAPIError as exc:
                s.rollback()
                set_status(s, "error", f"Reading the inbox failed: {exc}")
                return {"connected": True, "error": str(exc)}
            except Exception as exc:  # network blips etc. must not kill the loop
                s.rollback()
                set_status(s, "error", f"Reading the inbox failed: {exc.__class__.__name__}: {exc}")
                log.exception("gmail poll failed")
                return {"connected": True, "error": str(exc)}
    finally:
        _lock.release()


async def run_forever() -> None:
    while True:
        await asyncio.to_thread(poll_gmail_now)
        await asyncio.sleep(POLL_SECONDS)
