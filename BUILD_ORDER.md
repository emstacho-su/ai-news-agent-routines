# BUILD_ORDER.md — v1-routines

Phased build plan. Mirrors v1's `BUILD_ORDER.md` shape and the R0–R9
layout in `docs/routines-version-plan.md` §7. Each phase produces a
working state. Do not start the next phase until the current phase
verifies.

> **Canonical reference:** when this file and `docs/routines-version-plan.md`
> disagree, the planning doc wins. This file is the working checklist;
> the planning doc is the contract.

---

## Phase R0 — Repo bootstrap

**Goal:** new repo skeleton on disk and on GitHub, ready for Routines work.

- [x] Decide repo location (decided: new repo `ai-news-agent-routines`)
- [x] Create directory tree (`routines/`, `mcp-servers/resend/`, `data/`, `tests/`, `docs/`)
- [x] Copy verbatim from v1: `prompts/`, `static/`, `profile.py`, `dashboard.py`, `Dockerfile`, `.dockerignore`
- [x] Write fresh: `requirements.txt` (no anthropic, no APScheduler), `pyproject.toml`, `config.py` (slim), `fly.dev.toml` (new app, no volume), `.env.example`, `.gitignore`
- [x] Write fresh docs: `CLAUDE.md`, `BUILD_ORDER.md` (this file), `SPEC.md` (thin pointer), `README.md`
- [x] Copy `docs/routines-version-plan.md` from v1 into this repo
- [ ] `git init`, initial commit on `main`, branch `dev`
- [ ] `gh repo create emstacho-su/ai-news-agent-routines --public --source=.`
- [ ] Push both branches (with Stack's go-ahead on the push)

**Verification:** new repo visible on GitHub at `emstacho-su/ai-news-agent-routines` with `main` and `dev` branches and the bootstrap commit.

---

## Phase R1 — Routines onboarding + auth

**Goal:** three routines exist on Stack's Max plan with placeholder prompts and HTTP triggers enabled (where applicable).

- [ ] Authenticate Claude Code locally to the Max plan
- [ ] `claude routines create` for daily / follow-up / custom
- [ ] Save Routine IDs in `.env` and document in `.env.example`
- [ ] Verify each routine appears in `claude routines list`
- [ ] Note Routines API base URL and auth scheme in `config.py`

**Verification:** `claude routines list` shows three routines; `.env` has all three IDs; mocked test calling `_fire_routine` with one of the IDs returns the canned response.

---

## Phase R2 — Resend MCP server

**Goal:** a minimal MCP server exposing `send_email(to, subject, html, text)` that the daily routine can call.

- [ ] Scaffold `mcp-servers/resend/` (TypeScript, Node 20+, MCP SDK)
- [ ] Implement `send_email` tool wrapping the Resend SDK
- [ ] Local test against a Resend test API key
- [ ] Decide deploy target: stdio (bundled in routine container) vs remote (Cloudflare Workers / Fly.io sidecar)
- [ ] Deploy and register the MCP server with the daily routine

**Verification:** local stdio test sends a real email to `NOTIFY_TO_EMAIL` (Stack-gated), and the MCP server responds with `{ id, status: "queued" }`.

---

## Phase R3 — daily routine end-to-end

**Goal:** daily routine produces a briefing, writes to the data branch, and emails Stack.

- [ ] Port `prompts/daily_briefing.txt` into `routines/daily.yaml`
- [ ] Routine reads `memory.json` from data branch on start
- [ ] Routine writes `briefings/{date}.md` and updated `memory.json` back to data branch
- [ ] Routine calls Resend MCP for email
- [ ] Manual `claude routines run` to verify (Stack-gated; uses Max quota)
- [ ] Schedule cron `0 12 * * *` UTC

**Verification:** manual run produces a briefing on the data branch and an email arrives at `NOTIFY_TO_EMAIL`. Cron schedule is visible in `claude routines list`.

---

## Phase R4 — follow-up routine

**Goal:** dashboard can ask a follow-up question and receive an answer.

- [ ] Port `prompts/follow_up.txt` into `routines/follow-up.yaml`
- [ ] HTTP trigger enabled
- [ ] `dashboard.py` `POST /follow-up` route fires the trigger and awaits result
- [ ] Smoke test against an existing 2026-05-07 briefing item

**Verification:** dashboard follow-up form returns a coherent answer with sources cited.

---

## Phase R5 — custom routine

**Goal:** dashboard can request a focused briefing on an arbitrary topic.

- [ ] Port `prompts/custom_briefing.txt` into `routines/custom.yaml`
- [ ] HTTP trigger enabled
- [ ] `dashboard.py` custom-briefing route fires the trigger
- [ ] Versioning logic (`_v2.md`, `_v3.md`) handled inside the routine

**Verification:** firing a custom briefing for a slug that already has a briefing today produces a `_v2.md` file on the data branch.

---

## Phase R6 — dashboard refactor

**Goal:** `dashboard.py` no longer imports v1 modules and reads briefings from the data branch.

- [ ] Strip imports of `agent`, `tools`, `scheduler`, `notifications`, `budget`, `follow_ups`, `memory`
- [ ] Strip `_run_daily_job`, `_run_custom_job`, SSE streams, in-process scheduler lifespan
- [ ] Strip `/api/budget`, `/api/scheduler`
- [ ] Replace `BRIEFINGS_DIR` reads with GitHub raw URL fetches over `httpx`
- [ ] New `_fire_routine(name, payload) -> dict` helper
- [ ] New routes: `POST /trigger/daily`, `POST /trigger/custom`, `POST /follow-up`
- [ ] Tests mock Routine HTTP triggers with `respx`

**Verification:** dashboard runs locally (`uvicorn dashboard:app`), serves the briefings list from data branch, fires routines via mock without touching Anthropic API. Pytest passes with 60%+ coverage on critical paths.

---

## Phase R7 — Fly redeploy

**Goal:** dashboard deployed to a new Fly app, separate from v1's Fly app.

- [ ] Slim Dockerfile (drop anthropic SDK install — none needed now)
- [ ] `fly.dev.toml` ready (already in repo, app `ai-news-agent-routines-dev`)
- [ ] CI workflow under `.github/workflows/deploy.yml` for push-to-main → fly deploy
- [ ] First deploy via `fly launch --no-deploy` then `fly deploy`
- [ ] Verify dashboard reads briefings from the data branch

**Verification:** `https://ai-news-agent-routines-dev.fly.dev` serves the dashboard, briefings list populated from the data branch, basic auth challenge works.

---

## Phase R8 — Cutover

**Goal:** v1-routines is the source of Stack's morning email; v1 stays running as fallback.

- [ ] Run v1-routines daily routine for 3 consecutive days successfully (no manual intervention)
- [ ] Disable v1's APScheduler (set `SCHEDULER_ENABLED=0` on v1's Fly app)
- [ ] Watch the next morning's auto-fire from v1-routines
- [ ] Verify email arrives and dashboard shows the briefing

**Verification:** 3 consecutive days of clean v1-routines runs after v1's scheduler is disabled.

---

## Phase R9 — Polish

**Goal:** portfolio-ready repo.

- [ ] `README.md` describing the Routines architecture and the v1 → v1-routines arc
- [ ] `docs/learning-notes.md` covering: why migrate, what was hard about Routines + MCP, what the educational value looks like in retrospect
- [ ] Architecture diagram (Mermaid in README) showing routine ↔ data branch ↔ dashboard ↔ MCP flow
- [ ] Cross-link from v1's README to this repo and vice versa

**Verification:** Stack walks through the README and confirms it stands on its own as a portfolio piece.

---

## v1-routines acceptance criteria

When all of these are true, v1-routines is "done":

1. Daily briefing email arrives at `NOTIFY_TO_EMAIL` at 12:00 UTC, written by the daily routine
2. Dashboard at the v1-routines Fly URL shows the briefing within 1 minute of the data branch commit
3. Follow-up Q&A works end-to-end against today's briefing
4. Custom briefing fires from the dashboard, lands on the data branch, and renders
5. Total LLM API spend in the past 30 days is $0 (Max plan quota only)
6. v1 is still deployed and runnable as the educational reference
7. README is clean enough that a portfolio reviewer can grok the arc without reading code
