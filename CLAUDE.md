# CLAUDE.md — ai-news-agent-routines

This file is read by Claude Code at the start of every session in this
repository. It contains the operating rules, conventions, and guardrails
for working on **v1-routines**, a parallel rebuild of the AI News Agent
on Claude Code Routines.

---

## What this project is

The Routines edition of the AI News Agent. Same product surface as v1
(daily AI briefing email, dashboard, follow-up Q&A, custom briefings,
persistent topic memory). Different runtime: every Anthropic-API-billed
LLM call is replaced by a Claude Code Routine running on Stack's Max
plan, dropping marginal LLM cost to $0.

This repo lives alongside, not on top of,
[`ai-news-agent`](https://github.com/emstacho-su/ai-news-agent) (v1).
v1 stays deployed and runnable as the educational reference. v1-routines
is the "I rebuilt it on managed primitives once I understood it" story.

The owner is Stack (Evan Stachowiak).

---

## Project documents — read these in order

1. **CLAUDE.md** *(this file)* — operating rules
2. **docs/routines-version-plan.md** — canonical architecture doc; the SPEC.md equivalent for this repo
3. **BUILD_ORDER.md** — phased build plan R0–R9 with verification steps
4. **SPEC.md** — thin pointer to the planning doc; component-level detail lives in v1's SPEC.md
5. **README.md** — public-facing description (built up over time, polished in Phase R9)

---

## The most important rule

**This repo is v1-routines. v1 is a separate repo that stays deployed
during the entire build.**

That means:

- Do not edit anything under `C:\Users\estac\projects\ai-news-agent`
  while working in this repo. If a change in *this* repo requires a
  v1-side change (e.g., disabling v1's APScheduler at cutover), flag
  it before making the change.
- Do not import code from v1. The two are independent runtimes.
- Do not assume v1 conventions still apply to v1-routines. The agent
  loop is gone. The tool registry is gone. The budget tracker is gone.
  Those abstractions live in v1's portfolio narrative; here we lean on
  Routines, MCP servers, and the GitHub data branch instead.

The v1 / v2 rule from v1's CLAUDE.md is also in force here: V2_VISION.md
features (3D portfolio dashboard, OAuth, Slack/Discord delivery, etc.)
are off-limits unless Stack explicitly opens them.

---

## Build process — phase discipline

Development follows the 10 phases (R0–R9) in `BUILD_ORDER.md`, in
order, without skipping.

- Each phase has explicit verification steps; do not move on until the
  current phase verifies.
- Phases R0–R2 are infrastructure: repo bootstrap, Routines onboarding,
  MCP server. Move slowly here — Routines and MCP are new ground for
  Stack and the educational value is in understanding them.
- Phases R3–R5 port the three routines. Daily first, then follow-up,
  then custom.
- Phase R6 refactors `dashboard.py`. It is currently copied verbatim
  from v1 and will not run cleanly until R6 is done.
- Phases R7–R8 deploy and cut over.
- Phase R9 is portfolio polish.

If the planning doc is ambiguous, ask Stack rather than guessing. If
you discover the planning doc is wrong, propose a correction explicitly
rather than silently deviating.

---

## Code conventions

### Language & tooling
- Python 3.11+
- `uv` for venv + installs (`uv venv`, `uv pip install`, `uv run`)
- `ruff` for lint + format (config in `pyproject.toml`)
- Type hints on public functions and dataclasses
- pytest for tests, `respx` for mocking Routine HTTP triggers
- The Resend MCP server (Phase R2) is TypeScript / Node, not Python

### Project structure
```
.
├── config.py              # constants + env-loaded IDs
├── dashboard.py           # FastAPI; full refactor in R6
├── profile.py             # carried over from v1
├── prompts/               # source-of-truth prompts (mirrored into routines/*.yaml)
├── routines/              # *.yaml routine definitions (R1)
├── mcp-servers/resend/    # Resend MCP server source (R2)
├── static/                # HTML/CSS/JS (carried over from v1)
├── tests/                 # pytest suite
└── docs/                  # planning doc + learning notes
```

### Tests
- pytest, target ~60% coverage on critical paths
- Critical paths: `_fire_routine` helper, dashboard auth, GitHub data
  branch fetch logic, MCP server tool dispatch
- Mock Routine HTTP triggers with `respx` or `httpx.MockTransport`.
  Never hit real Routines in tests.
- Tests live in `tests/`, named `test_<module>.py`

### Logging
- Verbose by default (`config.VERBOSE_LOGGING = True`)
- Every Routine trigger logged: routine ID, payload (truncated), result
- Routine session URL (when returned) logged for trace debugging

### Git
- Conventional commits (`feat:` / `fix:` / `chore:` / `docs:` / `refactor:` / `test:` / `perf:` / `ci:`)
- Each commit message must be self-explanatory from the log alone, since
  Stack reviews on GitHub rather than approving each commit inline.
  Body explains *why*, not just *what*.
- `main` is prod, `dev` is dev/staging
- CI/CD on push to `main` (configured in Phase R7)
- **Before each push to `origin`, briefly tell Stack what is being
  pushed and why so he can object before it lands on the remote.**

---

## Conversation conventions with Stack

### Pacing
- Work diligently with short explanations as you go
- Redirect ambiguous decisions to Stack with options, do not guess
- Do not over-explain when implementing per the planning doc

### Cost awareness
- Stack has a Claude Max plan; v1-routines runs on its bundled quota.
  $0 marginal LLM cost, but quota is finite.
- **Do not trigger a real Routine run during development without
  Stack's explicit go-ahead.** Even though it doesn't bill API credits,
  it consumes Max plan tokens and tests should mock the trigger.
- Mocked tests + `claude routines run --dry-run` are the dev path.
  Real runs gate on Stack's "go".

### Style
- No emoji in code, comments, or commit messages (dashboard renders
  Lucide SVG icons, not emoji)
- Markdown briefings keep `[NEW]` / `[UPDATE]` text labels

---

## Things to never do without explicit permission

- Trigger a real Routine HTTP request in tests or during development
- Commit to `main` directly (always go through `dev` first, then merge)
- Modify v1 (the other repo) from this session
- Add a v2 feature
- Add a dependency not listed in `requirements.txt` or the planning doc
- Bypass the Stack-confirmation gate before pushing to `origin`
- Introduce a frontend framework (React, Vue, Svelte) — vanilla JS is
  the spec, same as v1
- Re-port v1 modules that the planning doc says are deprecated
  (`agent.py`, `tools.py`, `scheduler.py`, `budget.py`, `notifications.py`,
  `follow_ups.py`, `memory.py`, `main.py`)

---

## When something is unclear

Default to asking Stack. A good question is "Planning doc §X says Y but
I'm seeing Z because of W. Should I [option 1] or [option 2]?". A bad
question is "What should I do?" without context.

---

## Closing note

The hand-rolled agent loop in v1 is the "I built an agent" portfolio
piece. v1-routines is the "I rebuilt it on managed primitives once I
understood it" piece. Both are valid. Don't argue for keeping the
hand-rolled loop here — that ship sailed when Stack greenlit the
Routines plan.

When you make a non-obvious design choice (especially around Routines
APIs, MCP server design, or the data branch read pattern), briefly
explain *why*. Educational value is the point.
