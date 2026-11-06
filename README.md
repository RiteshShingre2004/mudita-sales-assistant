# Mudita AI Generalist assignments

**Assignment 01: Lumen sales assistant.** A working prototype for a fictional analytics
consultancy, Lumen Analytics Co. It turns inquiries from Instagram, WhatsApp and email into
qualified, assigned leads and confirmed calendar meetings, with a human able to take over at
any point.

**Assignment 02: three connected mini-agents.** Being built in the same application under its
own route (`/agents`), as the brief allows. This README gains its section when it is done.

---

## Live demo and access

| | |
|---|---|
| Dashboard | https://lumen-sales-assistant.onrender.com |
| Sign-in | user `admin`; the password is in the submission email (never in this repository) |
| Public pages | [/about](https://lumen-sales-assistant.onrender.com/about), [/privacy](https://lumen-sales-assistant.onrender.com/privacy), [/terms](https://lumen-sales-assistant.onrender.com/terms), [/data-deletion](https://lumen-sales-assistant.onrender.com/data-deletion), [/healthz](https://lumen-sales-assistant.onrender.com/healthz) |
| Try it yourself | email **riteshshingre+lumen@gmail.com** from any address and watch the inbox. Instagram and WhatsApp only answer authorised test accounts (see below). |

The app is kept awake by an UptimeRobot check on `/healthz` every 5 minutes (Render's free plan
otherwise sleeps after 15 minutes and would stop reading the inbox).

## Assignment 01: what was shown working, live

| Brief requirement | Result | Evidence |
|---|---|---|
| A real inquiry arrives from **email** | ✅ | Gmail thread, qualified, routed to Daniel Brooks |
| A real inquiry arrives from **Instagram** | ✅ | DMs from a tester account, qualified, routed to Daniel Brooks |
| A real inquiry arrives from **WhatsApp** | ✅ inbound / ❌ outbound | Messages arrive and are qualified (Priya Nair); Meta blocks replies; see [WhatsApp blocker](#whatsapp-blocker) |
| Qualification, routing and a **confirmed calendar booking** | ✅ | Email and Instagram leads each booked a real Google Calendar event |
| An incomplete inquiry gets a **useful follow-up** | ✅ | Asks for at most two missing facts; hands over to a person after 4 questions |
| "Ignore your rules" **does not override policy** | ✅ | Live on WhatsApp: "budget unlimited" rejected (no amount in the quote), no rep, no booking, no CEO; plus an automated test with a fully compromised model answer |
| **Repeated confirmation** creates no duplicate | ✅ | Email and Instagram leads each confirmed twice: one event each, the second reply says "You're all set" |
| **Newly occupied slot** creates no conflict | ✅ (automated test) | Slot taken between offer and confirmation → "just taken" + fresh times, no event |

### WhatsApp blocker

**Incomplete: outbound WhatsApp replies are blocked by Meta. Inbound works end to end.**

- **Working:** WhatsApp Cloud API webhook (signature-checked), messages from the authorised test
  phone arrive, are qualified, routed and logged, and the reply is written.
- **Blocked:** every send returns WhatsApp error **131031 "Business account has been locked"**.
  Meta's own `health_status` for the WhatsApp Business Account (`GET /{waba-id}?fields=health_status`)
  reports `can_send_message: BLOCKED` with:
  - **141006**: "There is an error with the payment method. This will block business initiated
    conversations."
  - **141010**: "The Business has not passed business verification."
  - (131000, an incomplete business profile, was also listed and was fixed.)
- **Attempted:** completed the business profile (cleared 131000); published the Meta app (required
  before Meta delivers any real webhooks); linked the WhatsApp account to the app
  (`POST /{waba-id}/subscribed_apps`); tried to add a payment method, but the Meta-created test
  WhatsApp account is not listed under the business portfolio, so there is nowhere to add one.
  Business verification needs registered-company documents, which a fictional company cannot
  honestly provide.
- **In the product:** the dashboard shows each failed reply with Meta's reason and flags the
  conversation **Needs you**, so the block is visible rather than silent.

## Assignment 01: how it works

### Stack

FastAPI (Python 3.12) · SQLAlchemy 2 · Postgres (SQLite locally) · Groq (`openai/gpt-oss-120b`,
`openai/gpt-oss-20b`) · vanilla JS dashboard · Render. No front-end build step.

### How a message becomes a decision

```
inbound message ─► stored once (dedupe by provider id) ─► human in control? ─yes─► stay quiet (logged)
        │no
        ▼
LLM: fill a form (service, budget, start, role, company, intent, slot choice) + an exact quote for each
        ▼
code: is every quote really in the lead's messages? are values on the policy's lists?
      (anything failing is dropped and logged as "evidence rejected"; verified facts carry over)
        ▼
code: policy rules ─► qualified / needs info / not qualified, with a reason per rule
        ▼
code: routing rules ─► rep (qualified only)
        ▼
code: next move ─ ask for ≤2 missing facts · decline once · hand to a person · offer 3 real slots · book
        ▼
LLM: word the reply from an allowed-facts list ─► code checks the draft ─► unsafe? use the template
        ▼
takeover guard again ─► send on the same channel ─► failure? flag "Needs you" with the reason
```

**The LLM has no authority.** It never sets a status, never picks a rep, never decides to book
and never writes meeting times. Its only outputs are a form of quoted facts and the wording of a
reply. Prompt injection is therefore contained by design, not by prompt wording: the automated
injection test feeds a fully compromised model answer (invented quotes, a fake
`"status": "qualified"` key, a reply claiming a booking with "our CEO") and checks that the lead
stays "needs info", no rep is assigned and the safe template is sent.

**Grounding.** Every fact in the lead panel shows the lead's own words; select the quote to see it
highlighted in the message. Reply drafts are rejected if they claim a booking, name another rep,
mention a title such as CEO, state an amount not in the policy, or contain links, emails or phone
numbers.

**Editable business policy.** `/policy` edits a YAML policy (services, qualifying criteria, two
reps, routing rules, timezone, working hours, meeting rules, currency rates). Every save is
validated (unknown keys, unknown reps or services, bad timezones are rejected), versioned, and used
by the next decision; each decision records the policy version it used.

**Human control.** Each conversation shows status, evidence, assigned rep, meeting and an action
log. **Take over** stops all automatic replies, slot offers and bookings; the check runs right
before each action, so work already in progress stops too. Operators can reply from the dashboard,
retry after a failure, and delete a conversation and its data.

### Integration research and choices

| Need | Chosen | Why | Alternatives considered |
|---|---|---|---|
| Email | Gmail API, polling the history cursor every 20 s | Exact and simple; push (Pub/Sub) still needs `history.list` and adds a topic and subscription | Gmail push; IMAP (no thread ids, app passwords) |
| WhatsApp | WhatsApp Cloud API, Meta test number | Official, free test number, allowed-recipient list matches "authorised test recipients" | Twilio (sandbox is a simulator-like shared number) |
| Instagram | Instagram messaging through the Facebook Page linked to the professional account (Facebook Login token) | Meta's "Unified login" issues Facebook-style tokens; the code detects the token type and uses the Page route; Instagram Login tokens also work | Instagram Login only (rejected the token type Meta issued) |
| Calendar | Google Calendar API | We can set the **event id**, which makes booking idempotent at the provider; free/busy for availability | Calendly / Cal.com (no client-chosen ids; booking pages, not an API we fully control) |
| LLM | Groq, gpt-oss-120b with strict JSON schema; gpt-oss-20b for wording | Strict schema output, low latency and cost | llama-3.3-70b (retired on Groq on 2026-08-16) |

**Channel details:**
- **Email:** only mail addressed to `EMAIL_INBOX_ADDRESS` (a `+lumen` alias), or in a thread
  already handled, is read, so the rest of the mailbox is never downloaded or stored. Auto-replies,
  lists, `no-reply@` and its own mail are ignored. Replies stay in the Gmail thread.
- **WhatsApp:** signature-checked webhook, stored before acknowledging, processed in the
  background, deduped by `wamid`; delivery receipts update each reply; Graph errors are explained
  in plain words.
- **Instagram:** the same webhook checks; echoes of our own messages are skipped; only usernames
  in `INSTAGRAM_ALLOWED_SENDERS` are answered, so the friends of a personal account never get
  automatic replies, and nothing from other senders is stored.

### Calendar booking

```
qualified ─► free/busy ─► 3 free slots (working hours, ≥4 h notice, spread across days,
             never a time on offer to someone else or already shown), listed by code
lead replies ─► "2" / "option 2" / "the second one" (code)  or a quoted natural choice (LLM)
             ─► vague ("yes", "1 or 2") → "which one?" · "none work" → different times
chosen slot ─► already booked for this lead? → "you're all set" (no new event)
            ─► re-check free/busy → taken since offered? → "just taken" + fresh times
            ─► takeover guard ─► booking row (pending)
            ─► create event with id = hash(conversation, slot)
                 200 → confirmed · 409 → already exists → confirmed
                 timeout → uncertain → GET that id: found → confirmed;
                           verified absent → create again with the same id
```

- **No duplicates:** the booking key is unique in the database and is the Google event id, so
  even two racing confirmations produce one event.
- **No conflicts:** availability is re-checked immediately before creating.
- **No blind retries:** an uncertain create is settled by looking the event up first.

### Integrity rules

- **No cross-channel merging:** contacts are unique per `(channel, sender id)`.
- **No duplicate messages:** unique per `(channel, provider message id)`, so re-delivered webhooks
  and re-polled mail are stored once.
- **No duplicate bookings:** see above.
- **Context survives refreshes and restarts:** everything is in Postgres; the open conversation is
  in the URL.
- **No transaction is held across a model call,** so a Take over click always lands.

### Failures are visible

LLM outage, bad key or malformed output (after one repair attempt), Google sign-in expiry, Meta
token or permission errors, a wrong webhook secret, a calendar that cannot be read, an uncertain
booking: each one sends nothing misleading, flags the conversation **Needs you** with the reason
and a **Retry** button, and turns the affected connection red in the Connections menu.

## Code layout (Assignment 01)

| Module | Responsibility |
|---|---|
| `app/policy.py`, `app/default_policy.yaml` | Policy schema, validation, versioned storage |
| `app/models.py`, `app/migrate.py` | Tables and integrity rules; add-missing-columns on start-up |
| `app/inbound.py`, `app/outbound.py` | One entry and one exit for all channels; dedupe; send status |
| `app/channels/gmail.py`, `whatsapp.py`, `instagram.py` | Channel adapters |
| `app/webhooks.py`, `app/meta.py` | Meta webhooks, signatures, Graph API errors, connection health |
| `app/google_oauth.py`, `app/poller.py` | Google sign-in and token refresh; inbox polling |
| `app/llm.py` | Groq client: timeouts, one safe retry, token usage and cost |
| `app/extraction.py` | LLM form-filling and quote verification |
| `app/rules.py`, `app/routing.py` | Qualification and routing: plain code |
| `app/replies.py` | Reply wording, draft checks, templates |
| `app/gcal.py`, `app/booking.py` | Free/busy, slots, idempotent booking |
| `app/pipeline.py` | The per-message flow above, with a per-conversation lock |
| `app/guards.py` | Takeover check before every automatic action |
| `app/api.py`, `app/static`, `app/templates` | Dashboard API and UI |

## Tests

`pytest -q` runs 127 tests in about 5 seconds, against in-memory fakes of Google, Groq and Meta
(`tests/fake_google.py`, `tests/fake_groq.py`, and fake Graph APIs inside the WhatsApp and Instagram
tests). The fakes exist only in tests; the running app always talks to the real APIs. Tests named
`test_REQUIRED_*` cover the brief's required cases: the full booking run, prompt injection,
takeover during processing, repeated confirmation, and a newly occupied slot.

## Cost per run

Measured from the live action log (each `lead_assessed` entry stores token counts and cost):

- **Extraction**, gpt-oss-120b: 1,000–1,300 input and 300–350 output tokens, **$0.00034–0.00040
  per inbound message**.
- **Reply wording**, gpt-oss-20b: about 300 input and 100 output tokens, about **$0.00005**.
- **A whole lead** (3–5 messages to a booking): about **$0.002**. Prices: Groq, November 2026.

## Bugs diagnosed along the way

- **Take over could be blocked while the model was thinking.** The pipeline held a database
  transaction across the LLM call. Found by a test that simulates an operator clicking Take over
  mid-call; fixed by committing before every model call.
- **An injection message erased a qualified lead's facts.** Seen live: the model returned blanks
  for earlier facts, and the lead dropped to "needs info". Fixed: verified facts carry over unless
  the lead corrects them (corrections still win).
- **Meta delivered no real webhooks.** Diagnosed from the dashboard's "last message from Meta"
  indicator and Meta's test button: the Meta app was unpublished. Fixed by adding privacy and
  data-deletion pages and publishing.
- **Instagram tokens from Meta's new "Unified login"** were Facebook-style, not Instagram Login
  tokens; added detection and the Facebook Page route.
- **A red banner from one conversation showed on another.** A CSS `display` rule overrode the
  `hidden` attribute.
- **Uptime monitor reported the app down** while it was up: the monitor checks with `HEAD`; the
  health route now answers it.

## Known limitations

- WhatsApp replies are blocked by Meta (see above).
- Instagram: the Page subscription call needs `pages_manage_metadata`, which this app's use case
  does not offer; DMs arrive anyway, and the app says so rather than raising an alarm.
- The Google OAuth app is published but unverified, so its consent screen shows Google's
  "unverified app" warning. That is fine for the developer's own test account.
- One meeting per lead; rescheduling and cancelling are out of scope.
- Availability comes from one test calendar standing in for the team; the fictional reps have
  no calendars of their own (the rep is named in the event).
- Slots are in the business time zone (IST), labelled. Budgets in other currencies use the
  policy's fixed rates.
- A single Render instance: the per-conversation lock is in-process.
- No full migrations: tables are created on start-up, and missing columns are added.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env            # optional; defaults work for local runs
python -m scripts.seed_demo     # optional: sample conversations, clearly labelled "Sample data"
uvicorn app.main:app --reload   # http://localhost:8000
pytest -q
```

## Deploy on Render

1. Push this repository to GitHub.
2. In Render choose **New > Blueprint** and select the repository. `render.yaml` creates the web
   service and a Postgres database.
3. Fill in the secret environment variables Render asks for.

## Environment variables

Names only; values live in Render, never in this repository.

| Group | Variables |
|---|---|
| App | `APP_ENV`, `DATABASE_URL`, `PUBLIC_BASE_URL`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD` |
| LLM | `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_REPLY_MODEL` |
| Google | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_CALENDAR_ID`, `EMAIL_INBOX_ADDRESS`, `GMAIL_POLL_SECONDS` |
| Meta | `META_APP_SECRET`, `META_VERIFY_TOKEN`, `META_GRAPH_VERSION` |
| WhatsApp | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` |
| Instagram | `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_ALLOWED_SENDERS` |

## AI tools used

Built with Claude (Anthropic) as a pair programmer for code, tests and documentation. Every
design decision and every line was reviewed, and the walkthrough explains the code.
