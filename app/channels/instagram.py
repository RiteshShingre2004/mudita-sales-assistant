"""Instagram Direct channel (Instagram API with Instagram Login).

Inbound: Meta calls POST /webhooks/instagram with `object: "instagram"` and a
`messaging` array. Our own sent messages come back as echoes (`is_echo`)
and are skipped. Messages are deduped by `mid`.

Two token types are supported, detected automatically by `link_account`:
- Instagram Login token (starts IGAA): calls go to graph.instagram.com/me/...
- Facebook Login / "Unified login" token (starts EAA): calls go to
  graph.facebook.com via the Facebook Page linked to the Instagram account,
  using that Page's access token.

Outbound: a text message to the lead's Instagram-scoped id (IGSID). Replies
are only allowed within 24 hours of the lead's last message; outside it the
error is explained in plain words.

`link_account` finds the professional account, subscribes it to message
webhooks and stores what it found, so no manual Graph API call is needed.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.actions import log_action
from app.config import settings
from app.inbound import InboundMessage, record_inbound
from app.meta import MetaAPIError, graph_post, set_health
from app.models import Connection, Contact, Conversation

PROVIDER = "instagram"
BASE = "https://graph.instagram.com"
FB_BASE = "https://graph.facebook.com"


def _account(session: Session) -> Connection:
    row = session.get(Connection, PROVIDER)
    if row is None:
        row = Connection(provider=PROVIDER, status="not_connected", credentials={}, state={})
        session.add(row)
        session.flush()
    return row


def _link_instagram_login(session: Session) -> dict:
    token = settings.instagram_token
    me = graph_post(session, PROVIDER, "me", token, None, base=BASE, method="GET",
                    params={"fields": "user_id,username"}, record_health=False)
    graph_post(session, PROVIDER, "me/subscribed_apps", token, None, base=BASE,
               params={"subscribed_fields": "messages"}, record_health=False)
    return {"mode": "instagram_login", "username": me.get("username", ""),
            "ig_user_id": str(me.get("user_id") or me.get("id", "")), "page_id": "", "page_token": ""}


def _link_facebook_login(session: Session) -> dict:
    token = settings.instagram_token
    ig_field = "instagram_business_account{id,username}"
    page = None
    try:  # a user or system-user token: look through the Pages it manages
        pages = graph_post(session, PROVIDER, "me/accounts", token, None, base=FB_BASE, method="GET",
                           params={"fields": f"id,name,access_token,{ig_field}"}, record_health=False)
        page = next((p for p in pages.get("data", []) if p.get("instagram_business_account")), None)
    except MetaAPIError:
        pass
    if page is None:  # maybe the token is itself a Page token
        me = graph_post(session, PROVIDER, "me", token, None, base=FB_BASE, method="GET",
                        params={"fields": f"id,name,{ig_field}"}, record_health=False)
        if me.get("instagram_business_account"):
            page = {**me, "access_token": token}
    if page is None:
        raise MetaAPIError("This token works, but no Facebook Page with a linked Instagram professional account "
                           "was found. Link your Instagram account to a Facebook Page, then generate the token again.")
    graph_post(session, PROVIDER, f"{page['id']}/subscribed_apps", page["access_token"], None, base=FB_BASE,
               params={"subscribed_fields": "messages"}, record_health=False)
    ig = page["instagram_business_account"]
    return {"mode": "facebook_login", "username": ig.get("username", ""), "ig_user_id": str(ig["id"]),
            "page_id": str(page["id"]), "page_token": page["access_token"]}


def link_account(session: Session, actor: str = "system") -> str:
    """Identify the professional account (either token type) and subscribe it to message webhooks."""
    if not settings.instagram_token:
        raise MetaAPIError("Instagram is not set up on the server (INSTAGRAM_ACCESS_TOKEN missing)")
    try:
        found = _link_instagram_login(session)
    except MetaAPIError as first:
        try:
            found = _link_facebook_login(session)
        except MetaAPIError as second:
            reason = second.reason if "no Facebook Page" in second.reason else (
                f"Instagram rejected the token ({first.reason}) and so did Facebook ({second.reason}). "
                "Generate a new token on the Instagram API setup page.")
            set_health(session, PROVIDER, "error", reason)
            raise MetaAPIError(reason)
    row = _account(session)
    row.account = "@" + found["username"]
    row.state = {**(row.state or {}), "mode": found["mode"], "ig_user_id": found["ig_user_id"],
                 "page_id": found["page_id"]}
    row.credentials = {"page_token": found["page_token"]} if found["page_token"] else {}
    how = "Facebook Page" if found["mode"] == "facebook_login" else "Instagram Login"
    log_action(session, actor=actor, action="instagram_linked",
               summary=f"Instagram {row.account} linked via {how}; message webhooks subscribed")
    session.commit()
    set_health(session, PROVIDER, "ok")
    return row.account


def _route(session: Session) -> tuple[str, str, str]:
    """(base url, token, messages path) for the linked account's token type."""
    row = session.get(Connection, PROVIDER)
    state = (row.state or {}) if row else {}
    if state.get("mode") == "facebook_login" and (row.credentials or {}).get("page_token"):
        return FB_BASE, row.credentials["page_token"], f"{state['page_id']}/messages"
    return BASE, settings.instagram_token, "me/messages"


def _own_ids(session: Session) -> set[str]:
    row = session.get(Connection, PROVIDER)
    own = (row.state or {}).get("ig_user_id") if row else None
    return {own} if own else set()


def _body_of(message: dict) -> str:
    text = message.get("text") or ""
    attachments = message.get("attachments") or []
    if attachments and not text:
        kinds = {a.get("type", "file") for a in attachments}
        what = "a photo" if kinds <= {"image"} else "an attachment"
        return f"[Sent {what}; the assistant only reads text]"
    return text


def parse_webhook(payload: dict, own_ids: set[str]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    if payload.get("object") != "instagram":
        return out
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for ev in entry.get("messaging", []):
            message = ev.get("message")
            if not message or message.get("is_echo") or message.get("is_deleted") or message.get("is_unsupported"):
                continue  # our own sent messages, deletions, read receipts, reactions
            sender = str(ev.get("sender", {}).get("id", ""))
            if not sender or sender in own_ids or sender == account_id:
                continue
            ts = ev.get("timestamp")
            out.append(InboundMessage(
                channel="instagram", external_id=sender, display_name="",
                channel_message_id=message["mid"], body=_body_of(message)[:4000],
                sent_at=datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc) if ts else None,
                meta={"account_id": account_id},
            ))
    return out


def _lookup_username(session: Session, igsid: str) -> str:
    base, token, _ = _route(session)
    try:
        data = graph_post(session, PROVIDER, igsid, token, None, base=base, method="GET",
                          params={"fields": "username,name"}, record_health=False)
    except Exception:
        return ""
    return data.get("username") and "@" + data["username"] or data.get("name", "")


def store_webhook(session: Session, payload: dict) -> list[int]:
    to_process: list[int] = []
    messages = parse_webhook(payload, _own_ids(session))
    if messages:
        set_health(session, PROVIDER, "ok")
    for inbound in messages:
        stored = record_inbound(session, inbound, actor="webhook")
        if stored is None:
            continue
        conv, _ = stored
        contact = session.get(Contact, conv.contact_id)
        if contact.display_name == contact.external_id and settings.instagram_token:
            name = _lookup_username(session, contact.external_id)
            if name:
                contact.display_name = name
                session.commit()
        if conv.id not in to_process:
            to_process.append(conv.id)
    return to_process


def _trim_bytes(text: str, limit: int = 1000) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: limit - 3].decode("utf-8", errors="ignore").rstrip() + "..."


def send_instagram(session: Session, conv: Conversation, text: str) -> str:
    if not settings.instagram_token:
        raise MetaAPIError("Instagram is not set up on the server (INSTAGRAM_ACCESS_TOKEN missing)")
    contact = session.get(Contact, conv.contact_id)
    base, token, path = _route(session)
    data = graph_post(session, PROVIDER, path, token,
                      {"recipient": {"id": contact.external_id}, "message": {"text": _trim_bytes(text)}}, base=base)
    return data["message_id"]
