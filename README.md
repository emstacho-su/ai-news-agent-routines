# ai-news-agent-routines

A daily AI-news briefing agent rebuilt on **Claude Code Routines** + a
custom **Resend MCP server**, so it runs on a Claude Max subscription
quota with **$0 marginal LLM cost**.

This is the Routines edition of the
[`ai-news-agent`](https://github.com/emstacho-su/ai-news-agent) (v1)
project. Same product surface — daily AI briefing email, dashboard,
follow-up Q&A, custom briefings, persistent topic memory — different
runtime.

> **Status:** in active development. See [`BUILD_ORDER.md`](./BUILD_ORDER.md)
> for phase progress.

## Why two repos

- **v1** is the "I built an agent loop by hand" portfolio piece:
  hand-rolled tool dispatch, in-process scheduler, Anthropic SDK,
  Fly persistent volume.
- **v1-routines** (this repo) is the "I rebuilt it on managed
  primitives once I understood it" piece: YAML routine definitions,
  cron-fired daily run, MCP server for email, GitHub data branch as
  primary store.

Both ship. v1 stays deployed as the educational reference.

## Architecture (planned)

```
            ┌─────────────────┐
            │  Routine cron   │   12:00 UTC daily
            │ (daily.yaml)    │
            └────────┬────────┘
                     │
                     ▼
         ┌───────────────────────┐
         │  Claude Code Routine  │  reads + writes
         │  (web_search, files)  │◄────────────────────┐
         └────────────┬──────────┘                     │
                      │                                │
                      │ MCP call                       │
                      ▼                                │
              ┌───────────────┐                        │
              │ Resend MCP    │ → email to inbox        │
              └───────────────┘                        │
                                                       │
                                                       │
   GitHub `data` branch  ◄────────────────────────────┘
   (briefings/, custom_briefings/, follow_ups/, memory.json)
                ▲
                │ raw URL fetch
                │
   ┌────────────┴────────────┐
   │ Fly-hosted dashboard    │  basic auth, vanilla JS
   │ (dashboard.py)          │  fires routine HTTP triggers
   └─────────────────────────┘
```

## Layout

```
.
├── routines/              # YAML routine definitions (Phase R1+)
├── mcp-servers/resend/    # Resend MCP server source (Phase R2)
├── dashboard.py           # FastAPI dashboard (Phase R6 refactor)
├── prompts/               # source-of-truth prompts mirrored into routines
├── static/                # vanilla HTML/CSS/JS for the dashboard
├── tests/                 # pytest, mocks Routine HTTP triggers
├── docs/
│   └── routines-version-plan.md   # canonical architecture doc
├── CLAUDE.md              # operating rules for Claude Code in this repo
├── BUILD_ORDER.md         # phased build plan (R0–R9)
└── SPEC.md                # thin pointer to the planning doc
```

## Status

| Phase | Status |
|---|---|
| R0 — Repo bootstrap | in progress |
| R1 — Routines onboarding | not started |
| R2 — Resend MCP server | not started |
| R3 — Daily routine | not started |
| R4 — Follow-up routine | not started |
| R5 — Custom routine | not started |
| R6 — Dashboard refactor | not started |
| R7 — Fly redeploy | not started |
| R8 — Cutover | not started |
| R9 — Polish | not started |

(README will be expanded substantially in Phase R9.)
