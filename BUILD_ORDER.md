# BUILD_ORDER.md — v1-routines

Phased build plan. Mirrors v1's `BUILD_ORDER.md` shape and the R0–R9
layout in `docs/routines-version-plan.md` §7. Each phase produces a
working state. Do not start the next phase until the current phase
verifies.

> **Canonical reference:** when this file, `docs/r1-deviations.md`, and
> `docs/routines-version-plan.md` disagree, **r1-deviations.md wins**,
> then BUILD_ORDER.md, then the planning doc. The deviations doc records
> corrections to the planning doc that surfaced once the actual Routines
> API was inspected. The planning doc is preserved as a historical
> artifact.

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

## Phase R1 — Routines onboarding

**Goal:** two routines exist on Stack's Max plan with placeholder prompts.

- [ ] Map actual Routines API surface (done — see `docs/r1-deviations.md`)
- [ ] Create `ai-news-agent-daily` (cron `0 12 * * *` UTC) with placeholder prompt
- [ ] Create `ai-news-agent-processor` (cron `0 * * * *` UTC) with placeholder prompt
- [ ] Save the two routine IDs in local `.env` and document in `.env.example`
- [ ] Verify both routines appear via `RemoteTrigger {action: "list"}`
- [ ] Update `config.py` to use 2-routine constants (`ROUTINE_DAILY_ID`, `ROUTINE_PROCESSOR_ID`)

**Verification:** `RemoteTrigger {action: "list"}` shows two routines; both have `enabled: true` and the right cron expressions; `.env` has both IDs.

---

## Phase R2 — DELETED

The Resend MCP server is no longer needed. The daily routine attaches
the **Gmail connector** (`connector_uuid`
`468bd9e7-cae6-4c6c-8a3b-f9db61d8d737`) directly. See
`docs/r1-deviations.md` D1.

---

## Phase R3 — Daily routine end-to-end

**Goal:** daily routine produces a briefing and writes it to the data branch. (Email step dropped per `docs/r1-deviations.md` G2.5 and revised D1; the dashboard is the read surface.)

- [x] Port `prompts/daily_briefing.txt` into `routines/daily.prompt.md` and install on the routine
- [x] First run: briefing composed correctly, memory updated correctly, push failed (env had no git write creds)
- [x] Drop email step from prompt + remove Gmail connector from routine (revised D1 + G2.5)
- [x] Stack installed Claude GitHub App on `emstacho-su/ai-news-agent-routines` with Contents: Read+Write
- [x] Re-fired routine 2026-05-08T01:55Z; ~7 min runtime; commit `903addd` on data branch
- [x] Verified `briefings/2026-05-08.md` (10 stories, 5 sections) + `memory.json` (10 topics) committed by `emstacho-su` (GitHub App)
- [x] Smoke-read briefing — quality bar met (TrustFall, Routines preview, Colossus deal all caught)
- [x] Re-enabled cron; next auto-fire 2026-05-08T12:04:51 UTC

**Verification: PASSED.** Briefing readable at `https://raw.githubusercontent.com/emstacho-su/ai-news-agent-routines/data/briefings/2026-05-08.md`. Routine `enabled: true`, cron `0 12 * * *` UTC.

---

## Phase R4 — Processor routine (RETIRED post-R7)

**Original goal:** hourly processor routine handling follow-up + custom requests.

**Verified working (commit `16c8659`):** processed 2 test requests cleanly, schema correct.

**Then retired** when Stack flagged the Max plan's ~15 runs/day cap. An hourly cron blows the cap. Drain logic was folded into the daily routine instead — see D3 in `docs/r1-deviations.md` and the updated `routines/daily.prompt.md` Step 5.5.

The processor routine (`trig_016P6y3fNZp3utmDduv5tA6D`) is renamed `ai-news-agent-processor (RETIRED — merged into daily)` and `enabled: false`. The processor.prompt.md file is kept in the repo as a reference for the retired design.

Net result: **1 routine run per day** (the daily, which generates the briefing and drains up to 5 queued requests in the same run). Quota headroom: 14 runs/day for manual fires + safety margin.

---

## Phase R5 — DELETED

Merged into R4. The dashboard wiring (writing request files, polling for
responses) is owned by Phase R6. See `docs/r1-deviations.md` D3.

---

## Phase R6 — Dashboard refactor

**Goal:** `dashboard.py` no longer imports v1 modules; reads briefings from the data branch; writes request files for follow-up + custom and polls for responses.

- [x] Stripped imports of `agent`, `tools`, `scheduler`, `notifications`, `budget`, `follow_ups`, `memory`
- [x] Stripped `_run_daily_job`, `_run_custom_job`, SSE streams, in-process scheduler lifespan
- [x] Stripped `/api/budget`, `/api/scheduler`, `/trigger/daily`
- [x] Replaced `BRIEFINGS_DIR` reads with GitHub raw URL fetches over `httpx` (with TTL cache)
- [x] New `queue_follow_up` / `queue_custom_briefing` helpers that PUT request JSON to the data branch via Contents API
- [x] New `check_follow_up_response` / `find_custom_response` helpers that read from raw URLs / list via Contents API
- [x] New routes: `POST /briefing/{date}/item/{id}/ask`, `POST /trigger/custom`, `GET /follow-up/{id}/status`, `GET /custom/status/{id}`
- [x] Front-end: queued + 30s setInterval polling UX; markdown answers rendered as `<pre>` textContent (no innerHTML on responses)
- [x] Tests via `respx`: 18/18 pass
- [x] **Vercel host pivot:** saved/read/profile dropped from deployed surface (filesystem ephemeral); helpers stay in dashboard.py for a future data-branch-backed iteration

**Verification: PASSED.** Local smoke + pytest both green.

---

## Phase R7 — Vercel deploy

**Goal:** dashboard hosted on Vercel free tier, reading from data branch, queueing requests via GitHub Contents API.

- [x] Added `vercel.json` (route /static/* to static, everything else to api/index.py)
- [x] Added `api/index.py` re-exporting the FastAPI app
- [x] Moved deps from `requirements.txt` to `pyproject.toml` (Vercel's @vercel/python prefers pyproject)
- [x] Dropped Dockerfile, .dockerignore, fly.dev.toml (unused on Vercel)
- [x] CI workflow runs pytest only; Vercel GitHub integration handles deploys
- [x] First deploy via `vercel deploy --prod`; production at https://ai-news-agent-routines.vercel.app
- [x] Stack set `DASHBOARD_PASSWORD` + `GITHUB_PAT` in Vercel project settings; redeployed to pick them up

**Verification: PASSED.** /api/health reports `write_enabled: true`; auth gate accepts the password. Briefing list, follow-up queueing, and custom queueing all live at https://ai-news-agent-routines.vercel.app .

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
