# `data` branch — primary state store for v1-routines

This branch is **not** part of the application's code history. It is a
disconnected ("orphan") branch that holds runtime state for the AI News
Agent (Routines edition):

```
.
├── memory.json              # topic memory across all daily runs
├── briefings/               # daily briefings (markdown), one per day
├── custom_briefings/        # focus-area briefings, written by the processor
├── follow_ups/              # follow-up Q&A responses, written by the processor
├── requests/                # follow-up requests written by the dashboard
└── custom_requests/         # custom briefing requests written by the dashboard
```

## How writes happen

- **Daily routine** (`ai-news-agent-daily`, cron `0 12 * * *` UTC):
  reads `memory.json`, writes `briefings/{YYYY-MM-DD}.md`, updates
  `memory.json`, sends the briefing to `NOTIFY_TO_EMAIL` via the Gmail
  connector, commits, pushes.
- **Processor routine** (`ai-news-agent-processor`, cron `0 * * * *`
  UTC): reads `requests/` and `custom_requests/`, writes responses
  under `follow_ups/` or `custom_briefings/`, moves processed request
  files to a `processed/` subfolder, commits, pushes.
- **Dashboard** (`dashboard.py` on Fly): writes request files to
  `requests/` or `custom_requests/` when the user submits a follow-up
  question or requests a custom briefing.

## How reads happen

- **Dashboard** fetches briefing markdown via the GitHub raw URL
  (`https://raw.githubusercontent.com/emstacho-su/ai-news-agent-routines/data/...`)
  on demand.

See `docs/r1-deviations.md` and `docs/routines-version-plan.md` on
the `dev` / `main` branches for the full design rationale.
