# Lumen sales assistant (Mudita assignment 01)

A prototype sales assistant for a fictional analytics consultancy, Lumen Analytics Co.
It turns inquiries from Instagram, WhatsApp and email into qualified, assigned leads
and confirmed meetings, with a human able to take over at any point.

> Status: **Step 1 of 8 complete**: skeleton, data model, editable policy, inbox UI,
> action log, takeover guard, deploy config. Channels, LLM and calendar follow in later steps.
> This README grows with each step.

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

### Integrity rules enforced by the database

- **No cross-channel merging.** Contacts are unique per `(channel, external_id)`.
- **No duplicate messages.** Messages are unique per `(channel, channel_message_id)`, so
  a re-delivered webhook or re-polled email is stored once.
- **No duplicate bookings.** Bookings are unique per `idempotency_key`; the same key
  becomes the Google Calendar event id (Step 4).
- **Policy can't reference staff or services that don't exist.** Validation rejects the save
  and the previous version stays active.

## Environment variables

`APP_ENV`, `DATABASE_URL`, `PUBLIC_BASE_URL`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`,
`GROQ_API_KEY`, `GROQ_MODEL`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`META_APP_SECRET`, `META_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `INSTAGRAM_ACCESS_TOKEN`.

## Known limitations (so far)

- Tables are created with `create_all`, not migrations (fine for a prototype).
- Render's free web service sleeps after ~15 minutes idle. The first webhook after
  sleep is slow; Meta retries undelivered webhooks, and duplicates are dropped by the
  message uniqueness rule.

## AI tools used

Built with Claude (Anthropic) as a coding assistant; all code is reviewed and explained
in the walkthrough.
