# Lumen sales assistant (Mudita assignment 01)

A prototype sales assistant for a fictional analytics consultancy, Lumen Analytics Co.
It turns inquiries from Instagram, WhatsApp and email into qualified, assigned leads
and confirmed meetings, with a human able to take over at any point.

> Status: **Step 5 of 8 built (Instagram).** Step 1: skeleton, data model, editable policy, inbox UI,
> takeover guard. Step 2: Gmail inbox channel. Step 3: LLM extraction with verified evidence,
> rule-based qualification, routing and grounded replies. Step 4: WhatsApp Cloud API.
> Step 5: Instagram Direct. Next: calendar booking. This README grows with each step.

## Stack

FastAPI (Python 3.12) · SQLAlchemy 2 · Postgres (SQLite locally) · Groq LLM ·
vanilla JS dashboard · deployed on Render.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env            # optional; defaults work for local runs
python -m scripts.seed_demo     # optional: 3 conversations marked "Sample data"
uvicorn app.main:app --reload
# open http://localhost:8000
pytest -q
```

`python -m scripts.seed_demo --clear` removes the sample conversations. Seeding refuses
to run when `APP_ENV=production`. Sample rows are labelled in the UI and are never
presented as live channel integrations.

## Deploy on Render

1. Push this repository to GitHub.
2. In Render choose **New > Blueprint** and select the repository. `render.yaml`
   creates the web service and a Postgres database.
3. Fill in the secret environment variables Render asks for (at minimum
   `DASHBOARD_PASSWORD` and `PUBLIC_BASE_URL`).

## Architecture (so far)

| Module | Responsibility |
|---|---|
| `app/policy.py` + `default_policy.yaml` | Business policy schema, cross-reference validation, versioned storage. Reps table is synced from the policy. |
| `app/models.py` | Tables and the integrity rules below. |
| `app/guards.py` | `require_automation_allowed()`, called right before every automatic outbound action. |
| `app/actions.py` | Append-only action log. |
| `app/api.py` | Dashboard JSON API (behind HTTP Basic login). |
| `app/static`, `app/templates` | Inbox, conversation, lead file, policy editor. |
| `app/inbound.py` | One entry point for every inbound message on any channel: contact lookup, dedupe, logging. |
| `app/outbound.py` | Sends on the conversation's own channel; takeover guard for automatic messages; failed/uncertain sends are recorded, never silently retried. |
| `app/google_oauth.py` | Google sign-in, token refresh, expiry detection. |
| `app/channels/gmail.py` | Gmail polling and threaded replies. |
| `app/poller.py` | Background loop (every 20 s) plus "Check inbox now". |
| `app/llm.py` | Groq client: timeouts, one safe retry, token usage and cost, health shown in the UI. |
| `app/extraction.py` | LLM fills a form of facts, each with a quote; code verifies every quote. |
| `app/rules.py` | Qualification: plain code applying the policy. |
| `app/routing.py` | Picks the rep from the policy's routing rules. |
| `app/replies.py` | Code decides what to say; the LLM words it; the draft is checked or replaced by a template. |
| `app/pipeline.py` | Ties it together per inbound message, with a per-conversation lock. |
| `app/migrate.py` | Adds new columns to an existing database on start-up. |
| `app/meta.py` | Meta webhook signature check, Graph API calls, readable error codes, connection health. |
| `app/webhooks.py` | Public webhook endpoints: verification handshake, signature check, store, then process in the background. |
| `app/channels/whatsapp.py` | WhatsApp payload parsing, delivery receipts, sending. |
| `app/channels/instagram.py` | Instagram DM parsing (echo filtering), username lookup, sending, account linking. |

### How a message becomes a decision (Step 3)

```
inbound message ─► takeover? ─yes─► stay quiet (logged)
        │no
        ▼
LLM: fill the form (service, budget, start, role, company, intent) + exact quote for each
        ▼
code: is every quote really in the lead's messages? are values on the policy's lists?
      (anything failing is dropped and logged as "evidence rejected")
        ▼
code: policy rules ─► qualified / needs info / not qualified, with a reason per rule
        ▼
code: routing rules ─► rep (qualified only)
        ▼
code: next move (ask for ≤2 missing facts · decline once · introduce rep · hand to a person)
        ▼
LLM: word it from an allowed-facts list ─► code checks the draft ─► unsafe? use the template
        ▼
takeover guard again ─► send on the same channel
```

**The LLM has no authority.** It never sees or returns a status, never chooses a rep and
never decides to book. Its only outputs are a form of facts and the wording of a reply.
That is why prompt injection is contained by design rather than by prompt wording: the
injection test feeds a *fully compromised* model answer (invented quotes, a fake
`"status": "qualified"` key, a reply claiming a booking with "our CEO") and checks that the
lead stays "needs info", no rep is assigned and the safe template is sent.

**Grounding.** Every fact shown in the lead panel carries the lead's own words; select the
quote to see it highlighted in the conversation. Reply drafts are rejected if they claim a
booking, name a rep other than the assigned one, mention a job title like CEO, state an
amount not in the policy, or contain links, emails or phone numbers.

**Failures are visible.** If Groq is down, rate-limited, returns malformed output twice, or
the key is wrong, nothing is sent; the conversation is flagged **Needs you** with the reason,
the LLM connection turns red, and **Retry assistant** re-runs it once fixed. After
`max_follow_up_questions` unanswered questions the assistant hands over to a person.

**Models.** `openai/gpt-oss-120b` for extraction with strict JSON-schema output, and
`openai/gpt-oss-20b` for reply wording. (`llama-3.3-70b-versatile` was retired on Groq on
2026-08-16.) Both are configurable with `GROQ_MODEL` and `GROQ_REPLY_MODEL`.

**Cost per inbound message (estimate).** About 1,500 input and 300 output tokens for
extraction on gpt-oss-120b (~$0.0004) plus about 300 input and 100 output tokens for the reply
on gpt-oss-20b (~$0.00005): **under $0.001 per message**, roughly **$0.002–0.004 per lead**
over a 3–5 message conversation. Actual token counts and cost for every call are stored in the
action log (`lead_assessed` entry, "llm_usage").

### Email channel: approach

**Polling Gmail's history API rather than push.** Gmail push needs a Pub/Sub topic and
subscription, and the push message only says *something* changed, so you still call
`history.list`. Polling the same history cursor every 20 seconds is simpler and just as
exact. The cursor advances only after every message in a batch is stored; a failure
mid-batch repeats the batch next time, and the dedupe rule drops what was already stored.

Mail that is never answered: our own sent mail, drafts, spam, auto-replies
(`Auto-Submitted`), bulk or list mail (`Precedence`, `List-Id`, `List-Unsubscribe`) and
`no-reply@` senders. Replies keep the Gmail thread (`threadId`, `In-Reply-To`,
`References`) and quoted earlier text is stripped before the message is stored.

**Personal mailbox safety.** With `EMAIL_INBOX_ADDRESS` set (for example `you+lumen@gmail.com`),
only mail addressed to it, or replies in a thread the assistant already handles, is treated
as an inquiry. Other mail is checked by its address headers only, never downloaded, stored
or logged. Replies carry that address as `Reply-To`.

One consent screen grants Gmail and Calendar access for the same test account. If a token
refresh returns `invalid_grant`, the connection is marked **Sign-in expired**, the top bar
turns red and a toast appears; a Reconnect button is in the Connections menu.

### WhatsApp channel (Step 4)

WhatsApp Cloud API with Meta's free **test number**, sending to recipients added to the test
number's allowed list (the brief's "authorised test senders").

- **Webhook** `GET/POST /webhooks/whatsapp`. The GET handshake checks `META_VERIFY_TOKEN`.
  Every POST must carry a valid `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with
  `META_APP_SECRET`); unsigned or forged calls get 401 and are logged.
- **Fast acknowledgement.** New messages are stored (deduped by `wamid`) and Meta gets its
  200 immediately; the LLM pipeline runs afterwards in the background, so a slow model
  never causes Meta to re-deliver. If it does re-deliver, the duplicate is dropped.
- **Non-text messages** (photos, voice notes) are stored with a note saying the assistant
  only reads text, and their captions are kept. Emoji reactions are ignored.
- **Delivery receipts** update each reply (sent, delivered, read). A `failed` receipt marks
  the reply failed and flags the conversation with the reason.
- **Readable errors.** Graph error codes become plain explanations in the UI, for example
  131047 (outside the 24-hour window, so only templates are allowed), 131030 (number not on
  the test number's allowed list) and 190 (token expired; the WhatsApp connection turns red).
- Only messages for our own `phone_number_id` are processed.

**24-hour rule.** WhatsApp allows free-form replies only within 24 hours of the lead's last
message. The assistant always answers inside that window. An operator reply after 24 hours
fails with a clear explanation; sending approved templates is out of scope for this prototype.

### Instagram channel (Step 5)

Instagram API with Instagram Login, for an Instagram **professional** account, with a
second account added as an **Instagram tester** on the Meta app.

- **Webhook** `GET/POST /webhooks/instagram`, same handshake and signature check as WhatsApp.
  Meta's documentation is ambiguous about which secret signs Instagram Login webhooks, so the
  signature is accepted if it matches either `INSTAGRAM_APP_SECRET` or `META_APP_SECRET`.
- **Account linking is automatic.** On start-up (and from Connections > Link Instagram account)
  the app reads which professional account the token belongs to and calls
  `me/subscribed_apps?subscribed_fields=messages`, so no manual Graph API call is needed.
- **Echoes are skipped.** Instagram sends our own replies back as `is_echo` messages; they are
  ignored, as are deletions, reactions and read receipts. DMs are deduped by `mid`.
- The sender's `@username` is looked up for display; if that fails the IGSID is shown.
- Replies are trimmed to Instagram's 1,000-byte limit. A reply outside the 24-hour window
  (error subcode 2018278) is explained in plain words and the conversation is flagged.

### Integrity rules enforced by the database

- **No cross-channel merging.** Contacts are unique per `(channel, external_id)`.
- **No duplicate messages.** Messages are unique per `(channel, channel_message_id)`, so
  a re-delivered webhook or re-polled email is stored once.
- **No duplicate bookings.** Bookings are unique per `idempotency_key`; the same key
  becomes the Google Calendar event id (Step 4).
- **Policy can't reference staff or services that don't exist.** Validation rejects the save
  and the previous version stays active. Unknown or misspelled keys are rejected too.

## Environment variables

`APP_ENV`, `DATABASE_URL`, `PUBLIC_BASE_URL`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`,
`GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_REPLY_MODEL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `EMAIL_INBOX_ADDRESS`, `GMAIL_POLL_SECONDS`,
`META_APP_SECRET`, `META_VERIFY_TOKEN`, `META_GRAPH_VERSION` (default `v23.0`),
`WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_APP_SECRET`.

## Known limitations (so far)

- Tables are created with `create_all`, plus a small add-missing-columns step on start-up;
  there are no full migrations (fine for a prototype).
- The per-conversation lock is in-process. That is correct for the single Render instance;
  running several instances would need a database lock instead.
- Budgets in other currencies use fixed rates from the policy, not live exchange rates.
- Google refresh tokens for apps in **Testing** mode expire after 7 days. Reconnect Google
  from the Connections menu if the demo runs longer.
- Tests use in-memory fakes of Google's and Groq's endpoints (`tests/fake_google.py`,
  `tests/fake_groq.py`, and fake Graph APIs in `tests/test_whatsapp.py` and `tests/test_instagram.py`). That is test scaffolding only; the running app always talks to the
  real APIs.
- Render's free web service sleeps after ~15 minutes idle. The first webhook after
  sleep is slow; Meta retries undelivered webhooks, and duplicates are dropped by the
  message uniqueness rule.

## AI tools used

Built with Claude (Anthropic) as a coding assistant; all code is reviewed and explained
in the walkthrough.
