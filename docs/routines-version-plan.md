# AI News Agent — Routines version (planning doc)

Standalone planning artifact for a parallel version of the AI News Agent
that replaces all Anthropic-API-billed work with Claude Code Routines.
The current Fly.io + hand-rolled-loop implementation in this repo is
**v1**. This doc plans **v1-routines**, an alternate architecture with
the same product surface but $0 marginal LLM cost (under an existing
Claude Max plan).

**Status:** planning. No code yet. Stack will decide after reading this
whether to build in-repo (a `routines/` subtree) or fork to a new repo.

---

## 1. Why this exists

### 1.1 Cost driver

The hand-rolled v1 spends ~$0.92 per daily run + ~$0.05–0.15 per
follow-up Q&A click against the Anthropic API. Steady-state monthly
cost is ~$28–35 in API credits + ~$1.94 Fly.io always-on.

Stack already pays for a Claude Max plan for unrelated dev work. Claude
Code Routines run on the Max plan's bundled token quota, not against the
API. So **moving every LLM call from the API to a Routine drops API
spend to $0** and adds zero marginal subscription cost.

Annualized: ~$340/year saved.

### 1.2 What stays the same

The product surface is identical from a user's POV:

- Daily AI news briefing emailed at a fixed wall-clock time
- Web dashboard for reading briefings (auth-gated, single user)
- Follow-up Q&A on individual briefing items
- Custom briefings on a focus area
- Persistent topic memory so `[NEW]` vs `[UPDATE]` works
- Markdown briefings as the canonical artifact

### 1.3 What changes

The runtime. Specifically: Anthropic SDK → Claude Code Routines, in-
process APScheduler → Routine cron, Fly volume → GitHub `data` branch,
custom MCP server → Resend or Gmail integration.

### 1.4 What's deliberately given up

- The educational hand-rolled agent loop in `agent.py`. Routines abstract
  it away. Stack has explicitly accepted this trade — v1 already shipped
  and the loop already lives in the portfolio.
- Per-run-mode tool restrictions (the `FOLLOW_UP_TOOLS` / `CUSTOM_TOOLS`
  / `DAILY_TOOLS` split). Routines control tools at the routine
  definition layer, not by enforcing a Python registry.
- Direct token-cost observability (`data/cost_tracker.json`). Max
  plan dashboards report quota usage, not per-run cost.

---

## 2. Architectural comparison

| Concern | v1 (current) | v1-routines (planned) |
|---|---|---|
| Daily run | APScheduler in FastAPI process → `agent.py` loop → Anthropic API | Routine with cron schedule → built-in Claude Code tools |
| Q&A trigger | FastAPI route → narrowed agent loop → Anthropic API | FastAPI route → POST to Routine HTTP trigger → response |
| Custom briefing | Same pattern as daily, separate tool registry | Routine with parameterized prompt; user-supplied focus area |
| State (memory.json, briefings) | Fly persistent volume at `/app/data` | GitHub `data` branch, repo-controlled |
| Email | `notifications.py` calls Resend SDK from Fly process | Custom MCP server wrapping Resend, called from Routine |
| Auth (dashboard) | HTTP basic auth in FastAPI | Same — dashboard still served from Fly |
| Cost tracking | `budget.py` records per-run USD | Max plan quota dashboard (no per-run granularity) |
| Failure detection | Failure email via Resend on exception | Routine sends failure-mode notification (built-in or via MCP) |
| Cron skew | DST drift accepted (12:00 UTC = 7am EST / 8am EDT) | Same — Routines support timezone-aware cron, drift up to product |
| Observability | `/api/scheduler` route; `flyctl logs` | Routine session URL; Routines admin UI |

### 2.1 Components removed

- `agent.py` — replaced by Routine prompt
- `tools.py` — Routines have built-in `web_search`, file ops, MCP tools
- `scheduler.py` — Routine's cron field
- `notifications.py` — replaced by Resend MCP server (or removed)
- `budget.py` — no per-run cost to track; Max quota suffices
- `memory.py` (file I/O) — replaced by Routine reading/writing `data` branch
- `follow_ups.py` (loop) — replaced by Routine HTTP trigger
- `main.py` (CLI) — Routines are launch-on-demand
- `requirements.txt: anthropic` — Anthropic SDK no longer needed

### 2.2 Components retained

- `dashboard.py` — pared down to read-only view + Routine triggers
- `static/` — HTML/CSS/JS unchanged in spirit
- `Dockerfile` + `fly.dev.toml` — still need a host for the dashboard
- `prompts/` — prompts move into Routine YAML but the wording transfers

### 2.3 Components new

- `routines/daily.yaml` — cron-fired daily briefing routine
- `routines/follow-up.yaml` — HTTP-triggered Q&A routine
- `routines/custom.yaml` — HTTP-triggered focused-topic routine
- `mcp-servers/resend/` — minimal Node or Python MCP wrapping Resend (only if email kept)
- A small dashboard ↔ Routine HTTP shim in `dashboard.py` to fire triggers and stream/await results

---

## 3. State management — the `data` branch

Routines run in ephemeral environments and don't have access to Fly
volumes. State has to live somewhere git-accessible.

### 3.1 Branch layout

```
data branch (force-pushed by daily routine; readable by all)
├── briefings/
│   ├── 2026-05-07.md
│   ├── 2026-05-08.md
│   └── ...
├── custom_briefings/
│   ├── 2026-05-07_quantum.md
│   └── ...
├── follow_ups/
│   └── ...
└── memory.json
```

This already exists from v1's backup workflow. v1-routines makes the
`data` branch the **primary** store, not a backup.

### 3.2 Read pattern (dashboard)

The dashboard fetches markdown over the GitHub raw URL:
`https://raw.githubusercontent.com/emstacho-su/ai-news-agent/data/briefings/2026-05-07.md`

GitHub serves these CDN-cached. No auth needed if the repo is public; a
GitHub PAT in the dashboard's env if private.

Cache invalidation: each Routine run writes a new commit. Dashboard can
either poll the branch tip every minute or rely on user refresh.

### 3.3 Write pattern (Routine)

Each Routine starts with a `git pull data` to read existing memory and
prior briefings. After generation it writes new files and `git push
--force data`. A locking mechanism is unnecessary because:

- The daily Routine fires at a fixed cron, no concurrent runs
- Custom + follow-up Routines don't write to the same files (different
  paths)
- Worst-case race: two follow-ups committing at once → second fails
  with non-fast-forward → Routine retries

### 3.4 Memory format

`memory.json` schema unchanged from v1:

```json
{
  "topics": [
    {
      "id": "topic-slug-N",
      "title": "...",
      "first_seen": "2026-05-07",
      "last_updated": "2026-05-07",
      "summary": "...",
      "sources": ["..."]
    }
  ]
}
```

Routine prompts produce JSON tool-use outputs that the Routine then
serializes into this structure.

---

## 4. The three Routines

### 4.1 daily.yaml

```yaml
name: ai-news-agent-daily
schedule: "0 12 * * *"   # 12:00 UTC daily
timezone: UTC

tools:
  - web_search
  - file_read
  - file_write
  - mcp.resend.send_email

prompt: |
  You are an AI news research agent for Stack. Today is {{ now | date }}.

  1. Read /workspace/data/memory.json. This contains topics seen in
     prior briefings.
  2. Use web_search to find notable AI news from the last 24 hours.
  3. For each candidate topic, decide whether it is [NEW] (not in
     memory) or an [UPDATE] of an existing topic.
  4. Produce a markdown briefing following the structure in the system
     prompt template, written to
     /workspace/data/briefings/{{ now | date }}.md.
  5. Update memory.json with new and updated topics.
  6. Commit and push to the data branch.
  7. Email the briefing to {{ secrets.NOTIFY_TO_EMAIL }} via the resend
     MCP tool.

system_prompt_file: prompts/daily_briefing.txt
```

### 4.2 follow-up.yaml

```yaml
name: ai-news-agent-follow-up
http_trigger: enabled

tools:
  - web_search
  - file_read

prompt: |
  Stack has a question about an item from a previous briefing.

  Briefing date: {{ payload.date }}
  Item id:       {{ payload.item_id }}
  Question:      {{ payload.question }}

  1. Read /workspace/data/briefings/{{ payload.date }}.md and locate
     the item with id {{ payload.item_id }}.
  2. Use web_search if you need fresh information.
  3. Answer concisely. Cite sources inline.

system_prompt_file: prompts/follow_up.txt
```

The dashboard fires this via `POST /v1/routines/{id}/run` with the
payload as JSON. Routine returns the answer in the response body when
the session completes.

### 4.3 custom.yaml

```yaml
name: ai-news-agent-custom
http_trigger: enabled

tools:
  - web_search
  - file_read
  - file_write

prompt: |
  Produce a focused briefing on: {{ payload.focus }}.

  1. Read /workspace/data/memory.json (read-only — do not write).
  2. Search and synthesize.
  3. Write to /workspace/data/custom_briefings/{{ now | date }}_{{ payload.slug }}.md
     with collision-handling versioning (_v2, _v3, ...).
  4. Commit and push to the data branch.

system_prompt_file: prompts/custom_briefing.txt
```

---

## 5. Email handling

### 5.1 Option A: Resend MCP server (recommended)

Build a minimal MCP server (Node or Python) that exposes one tool:
`send_email(to, subject, html, text)`. Hosts it as a stdio MCP or a
remote MCP. Routines reference it in their `tools:` block.

Pros:
- Same email reliability as v1
- Stack keeps the existing Resend account + verified sender
- Cleanly testable in isolation

Cons:
- ~50 lines of code + MCP plumbing
- One more deploy target (could be packaged inside the dashboard image)

### 5.2 Option B: Drop email entirely

Daily run produces a briefing; user opens dashboard each morning. No
email at all.

Pros:
- Zero new infrastructure
- One fewer secret to rotate

Cons:
- Behavior change — Stack currently relies on the email as the morning
  trigger
- Loses the failure-notification path (failure routines could log to a
  GitHub issue instead, but that's still infrastructure)

### 5.3 Option C: GitHub Actions email shim

Routine commits to `data` branch. A separate GitHub Actions workflow
fires on push to `data` and emails the latest briefing via Resend.

Pros:
- Reuses the existing Phase 17 workflow shape
- Resend secret lives in GitHub repo secrets, not in a Routine

Cons:
- Decoupled — daily generation success is now two-step
- Latency: 1–3 min between briefing commit and email arrival

**Decision recommendation: A.** Building a tiny Resend MCP is a
genuinely useful learning artifact (MCP servers are a portfolio piece
in their own right) and keeps the system tight.

---

## 6. Dashboard refactor

The Fly-hosted dashboard stays alive but loses ~70% of its current
responsibilities.

### 6.1 What dashboard.py keeps

- HTTP basic auth middleware
- Routes to render briefings list, briefing view, custom view, profile
  editor, saved/read state
- `/api/budget` endpoint — adapted to read Max-plan quota if exposed,
  else removed (no longer relevant)
- `/api/state/{date}` and saved-items endpoints

### 6.2 What dashboard.py loses

- `_run_daily_job`, `_run_custom_job`, all SSE streams
- `/trigger/daily`, `/trigger/custom`, `/status/{job_id}/stream`
- Dependency on `agent.py`, `tools.py`, `scheduler.py`,
  `notifications.py`, `budget.py`, `follow_ups.py`
- The in-process APScheduler lifespan
- The Anthropic SDK requirement

### 6.3 What dashboard.py gains

- A `_fire_routine(name, payload) -> dict` helper that POSTs to the
  Routine HTTP trigger endpoint, awaits completion, returns the result
- New routes that use the helper:
  - `POST /trigger/daily` → fires `daily.yaml` (rare, mostly cron-fired)
  - `POST /trigger/custom` → fires `custom.yaml` with focus payload
  - `POST /follow-up` → fires `follow-up.yaml` with question payload
- A small change to the briefings-list logic: read from the GitHub
  `data` branch via raw URLs, not from `BRIEFINGS_DIR`

### 6.4 Streaming

v1 shows a live SSE activity stream during runs. Routines don't expose
event-by-event streaming; they expose a session URL that Stack can open
to see Claude's transcript.

Replacement UX:
- Dashboard fires the trigger
- Shows "running…" spinner (the one we just built)
- On completion, displays the result
- Session URL exposed as a "view trace" link for debugging

This is a UX downgrade from the current SSE log but acceptable given the
cost win.

---

## 7. Phased build plan

Mirrors BUILD_ORDER.md style. Each phase produces a working state.

### Phase R0 — Repo bootstrap

- [ ] Decide: in-repo `routines/` subtree, or new repo `ai-news-agent-routines`
- [ ] If new repo: copy `prompts/`, `static/`, `dashboard.py`, profile/saved/read modules, `Dockerfile`, `fly.dev.toml`
- [ ] If in-repo: create `routines/` subdir + `dashboard-routines.py` parallel entry point
- [ ] Add `.routines/` to `.gitignore` (Routine-product-internal files)
- [ ] CLAUDE.md update reflecting the new architecture (or write a fresh CLAUDE.md if new repo)

### Phase R1 — Routines onboarding + auth

- [ ] Authenticate Claude Code locally to the Max plan
- [ ] `claude routines create` for the three routines (daily / follow-up / custom)
- [ ] Verify each fires manually (no real run yet — placeholder prompts)
- [ ] Save Routine IDs in `config.py`

### Phase R2 — Resend MCP server

- [ ] Decide implementation language (TypeScript or Python — pick TS for the Anthropic SDK ecosystem alignment)
- [ ] Implement `send_email` tool
- [ ] Local test against a Resend test key
- [ ] Deploy as remote MCP (Cloudflare Workers, Fly.io sidecar, or stdio in Routine container)

### Phase R3 — daily routine end-to-end

- [ ] Port `prompts/daily_briefing.txt` to the routine YAML
- [ ] Routine reads `memory.json` from data branch, writes briefing + memory back
- [ ] Routine fires Resend MCP for email
- [ ] Manual `claude routines run` to verify
- [ ] Schedule the cron at 12:00 UTC

### Phase R4 — follow-up routine

- [ ] Port `prompts/follow_up.txt` to YAML
- [ ] HTTP trigger live
- [ ] Dashboard route POSTs to it; await response
- [ ] Test against existing 2026-05-07 briefing

### Phase R5 — custom routine

- [ ] Port `prompts/custom_briefing.txt` to YAML
- [ ] HTTP trigger live
- [ ] Dashboard custom-briefings page calls it
- [ ] Verify versioning (`_v2.md` etc.) works

### Phase R6 — dashboard refactor

- [ ] Strip agent loop / scheduler / notifications / budget code
- [ ] Replace BRIEFINGS_DIR reads with GitHub raw URL fetches
- [ ] New `_fire_routine` helper
- [ ] Update tests to mock Routine HTTP triggers
- [ ] Local smoke run

### Phase R7 — Fly redeploy

- [ ] New Dockerfile (slimmer — no Anthropic SDK)
- [ ] New `fly.dev.toml` (still always-on for HTTP requests)
- [ ] Push to repo, CI deploys
- [ ] Verify dashboard reads briefings from the data branch

### Phase R8 — Cutover

- [ ] Disable v1's APScheduler (or shut down v1 entirely if forking)
- [ ] Watch first auto-fire of v1-routines
- [ ] Verify email arrives
- [ ] Verify briefing on dashboard

### Phase R9 — Polish

- [ ] README describing the Routines architecture
- [ ] docs/learning-notes.md covering: why migrate, what was hard,
  what's the educational value of MCP servers
- [ ] Architecture diagram updated

Estimated effort: 4–6 sessions of focused work, similar pacing to v1's
Phases 1–10.

---

## 8. Open questions to resolve before coding

These are flagged for Stack's call:

1. **Repo decision.** New repo or in-place subtree? See §10 below.
2. **Email path.** Resend MCP (recommended) vs drop email vs GH Actions shim?
3. **Dashboard host.** Keep on Fly always-on, or move to Cloudflare Pages / Vercel? (Cost: Fly $1.94/mo vs others $0/mo for static + light backend.)
4. **Memory format compatibility.** Bring v1's `memory.json` over wholesale, or start fresh? (Recommendation: copy. Topic continuity matters.)
5. **Auth.** Keep HTTP basic auth, or upgrade since we're rebuilding anyway? (Recommendation: keep — v2 already plans an OAuth upgrade.)
6. **Routine concurrency.** What happens if Stack triggers a custom briefing while the daily is still running? (Need to check Routines docs.)
7. **Session URL exposure.** Should the dashboard show a "view Claude session" link for transparency? (Recommendation: yes — fits the educational frame even though loop itself is hidden.)
8. **Token quota awareness.** Max plan has bundled limits. Daily Opus run for ~5 minutes likely uses a non-trivial chunk of monthly quota. Need to measure during Phase R3.

---

## 9. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Routines product churns (still new) | Medium | Pin to a Routines version; if APIs break, fall back to v1's still-deployed instance |
| Max plan quota gets exhausted by daily runs | Medium-low | Measure in Phase R3; if hot, swap Opus for Sonnet on daily |
| Routine cold-start latency makes Q&A feel slow | Medium | Accept it; Q&A is rare, not interactive-chat speed |
| GitHub `data` branch grows large | Low | Force-push pattern keeps it single-commit; reflog has history if needed |
| Resend MCP server adds operational surface | Low | Tiny stateless service; failure mode is "no email", briefing still ships |
| Stack cancels Max plan in the future | Low | Routines stop working; v1 fallback would still be in `main` of the original repo |
| Educational regret | Negotiated already | Stack accepted this trade explicitly |

---

## 10. Decision: in-repo vs new repo

This is the headline question to answer next.

### 10.1 In-repo (`routines/` subtree on `dev` branch of this repo)

**Pros:**
- Both versions live side-by-side — easy A/B comparison
- Shared assets (prompts, static files, profile.md) without duplication
- One CI/CD pipeline to maintain
- The decision log + git history reflect the evolution

**Cons:**
- Dual-stack codebase is harder to read for portfolio reviewers ("which one is real?")
- CLAUDE.md becomes a fork-by-fork-instructions doc
- Coupling: a refactor in `dashboard.py` for v1 might break v1-routines unexpectedly
- The "v1 is shipped, v1-routines is in progress" status is messier in commit history

### 10.2 New repo (`ai-news-agent-routines`)

**Pros:**
- Each repo is a clean portfolio piece on its own
- v1 stays frozen as "the educational hand-rolled version"; v1-routines is the "production-cheap version"
- Separate CI, separate deploy, separate Fly app — no cross-contamination
- Cleaner CLAUDE.md per repo
- Easy to walk a reviewer through "here's how I'd do it cheap" vs "here's how I learned the loop"

**Cons:**
- ~30% file duplication (prompts, static/, dashboard.py, profile, saved-items, Dockerfile)
- Two repos to maintain
- Memory.json sync between v1 and v1-routines during the cutover window is awkward (both writing to different stores)

### 10.3 Recommendation

**New repo, with a clean cutover.**

Reasoning:
- Portfolio framing is cleaner — v1 is the "I built an agent loop" story; v1-routines is the "I rebuilt it on managed primitives once I understood it" story. Both are good.
- v1 keeps running on Fly during the build of v1-routines. No risk of breaking the working system.
- The duplication is small (~6 files) and a one-time cost.
- A new repo lets v1-routines pick a different deploy target (Cloudflare, Vercel) without disturbing v1.

### 10.4 Migration sequence if going new-repo

1. Create `ai-news-agent-routines` repo
2. Copy: `prompts/`, `static/`, profile + saved + read modules, `Dockerfile`, `fly.dev.toml`, `tests/conftest.py` skeleton
3. Strip the unused-in-routines pieces during the copy
4. Walk through Phase R0–R9 in the new repo
5. When v1-routines is running stable for ~3 days, switch the cron in v1 to disabled (`SCHEDULER_ENABLED=0`) so only one source emails Stack each morning
6. Keep v1 deployed for ~30 days as a safety net before formally archiving

---

## 11. Next concrete step

Stack reads this doc and answers:

1. New repo or in-repo? (recommendation: new repo)
2. Email path? (recommendation: Resend MCP)
3. Dashboard host? (recommendation: keep Fly for now; reconsider in Phase R7)

Once those three are settled, we start Phase R0.
