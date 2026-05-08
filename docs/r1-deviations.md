# R1 deviations from the planning doc

`docs/routines-version-plan.md` was written before Stack and Claude had
ground-truth knowledge of the actual Claude Code Routines API. Phase R1
onboarding surfaced four mismatches between the doc's assumptions and
reality. This file records the corrected design and the reasoning, so
later phases work from accurate spec.

The planning doc itself stays unchanged as a historical artifact —
treat this file as the diff against it.

---

## Reality check (2026-05-07)

Routines run as remote CCR (Claude Code Remote) sessions in Anthropic's
cloud, with the following actual surface:

| Property | Reality |
|---|---|
| Trigger types | `cron_expression` (5-field UTC, **min 1-hour interval**) or `run_once_at` (RFC3339 UTC, future-only). **No HTTP trigger.** |
| Tools | Standard Claude Code tools: Bash, Read, Write, Edit, Glob, Grep, WebSearch, etc. Not the `web_search` / `file_read` / `file_write` named in the planning doc. |
| File access | Routine checks out one or more git repos as `sources` and operates on the working tree. No `/workspace/data/` raw paths. |
| MCP connections | Only **pre-registered** connectors from `claude.ai/customize/connectors` are usable, by `connector_uuid`. Custom URL-based MCP servers are not attachable directly. |
| Email | Gmail is already a registered connector — no custom email infrastructure needed. |

---

## Deviations and corrections

### D1. Resend MCP server removed

**Planned (§5):** Build a custom Resend MCP server (TypeScript), register
it, attach it to the daily routine.

**Corrected:** Use the **Gmail connector** (uuid
`468bd9e7-cae6-4c6c-8a3b-f9db61d8d737`) directly. Email comes from
Stack's actual Gmail account instead of `onboarding@resend.dev`.

**Effect on build plan:**
- Phase **R2 is deleted entirely**. ~50 lines of TypeScript + 1 deploy target eliminated.
- Email path is shorter and uses the inbox's native sender identity, which is a UX upgrade.
- Resend account remains usable for v1; v1-routines no longer depends on it.

### D2. No HTTP triggers — follow-up + custom redesigned as async polling

**Planned (§4.2, §4.3):** Dashboard fires routines via
`POST /v1/routines/{id}/run` and awaits the response inline.

**Corrected:** Routines fire only on cron or one-time. Follow-up and
custom briefings work as **async via data branch polling**:

1. User submits a follow-up question or custom briefing request from
   the dashboard.
2. Dashboard writes a request file (`requests/{ts}.json` or
   `custom_requests/{ts}.json`) to the `data` branch and pushes.
3. The hourly **processor routine** (cron `0 * * * *`) checks both
   request directories on each run, processes any new ones, writes
   responses (`follow_ups/{ts}.md` / `custom_briefings/{date}_{slug}.md`),
   and pushes back to the `data` branch.
4. Dashboard polls the data branch (or refreshes on user action) and
   renders the response when present.

**UX trade:** up to ~1 hour latency on follow-up and custom briefings,
versus the planned near-instant response. Quota cost: 24 polling runs/
day, most of which are empty no-ops. Acceptable given Max plan headroom.

### D3. Routine count changed from 3 to 2

**Planned (§4):** three routines — daily, follow-up, custom.

**Corrected:** **two routines** —

- `ai-news-agent-daily` — cron `0 12 * * *` UTC, generates and emails the daily briefing
- `ai-news-agent-processor` — cron `0 * * * *` UTC, processes both follow-up and custom requests on the data branch

The processor folds what the planning doc had as two separate routines
into one. Merging is cheaper on quota (one empty-poll run per hour
instead of two) and the two request types are similar enough that one
prompt can handle both via dispatch on file path.

**Effect on build plan:**
- Phase **R4** (was: follow-up routine port) is now: **processor
  routine — handles both follow-up and custom requests**.
- Phase **R5** is deleted; its dashboard-wiring concerns move into R6.

### D4. Tool surface clarified

**Planned (§4):** `tools: [web_search, file_read, file_write,
mcp.resend.send_email]`.

**Corrected:** Each routine declares an `allowed_tools` list using
standard Claude Code tool names. For both routines:

```
allowed_tools: ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch"]
```

Plus `mcp_connections` for the daily routine to attach Gmail.

File reads/writes hit the routine's checked-out working tree, not a
fixed path. The routine's source is the `ai-news-agent-routines` repo
itself; it pulls the `data` branch separately as part of its prompt.

---

## Updated phase scope

This is the corrected R0–R9 layout. The version in `BUILD_ORDER.md`
will mirror this.

- R0 — Repo bootstrap (done)
- R1 — Routines onboarding: create the 2 routines with placeholder
  prompts, save IDs, document API surface in `config.py`
- R2 — **DELETED** (Gmail connector replaces Resend MCP)
- R3 — Daily routine end-to-end: real prompt, news fetch, briefing
  written to data branch, Gmail email sent
- R4 — Processor routine: handles follow-up + custom request files on
  the data branch, writes responses back
- R5 — **DELETED** (merged into R4)
- R6 — Dashboard refactor: read from data branch via raw URLs; routes
  to write request files; polling logic for response readiness
- R7 — Fly redeploy on `ai-news-agent-routines-dev`
- R8 — Cutover (disable v1's APScheduler)
- R9 — Polish

---

## Implementation gotchas captured during R1

### G1. Omitting `mcp_connections` auto-attaches all registered connectors

When creating a routine via `RemoteTrigger {action: "create"}`, leaving
`mcp_connections` out of the body causes **every** connector registered
on the user's claude.ai account to be attached to the routine
(Gmail, Calendar, Drive, Linear, Microsoft 365, Supabase, Vercel — all
seven in Stack's case). To create a routine with no connectors, pass
`mcp_connections: []` explicitly. To create one with a specific subset,
pass the list.

This was caught on the processor routine, which was then locked down via
`{action: "update", body: {clear_mcp_connections: true}}`.

For R3/R4: always pass an explicit `mcp_connections` list when creating
or updating, even if empty.

### G2. Routines are created enabled by default

`enabled: true` is the default. Both R1 routines were initially created
with `enabled: true` and would have started firing on cron immediately
(the daily at next 12:00 UTC, the processor at next minute :00).
Because the placeholder prompts still consume Max-plan tokens, both
were **disabled** at the end of R1. Re-enable each routine when its
real prompt lands:

- `ai-news-agent-daily` (`trig_01SEgjfz9XX5nN5BDC9bKe5i`) — re-enable in **R3** after the briefing prompt is installed and a manual run verifies success.
- `ai-news-agent-processor` (`trig_016P6y3fNZp3utmDduv5tA6D`) — re-enable in **R4** after the request-processing prompt is installed and a manual run verifies success.

Re-enable command shape:
```
RemoteTrigger {action: "update", trigger_id: "<id>", body: {"enabled": true}}
```

---

## Things still TBD (will be settled in their own phases)

- Whether the hourly processor's empty-poll cost is meaningful against
  the Max quota. Measure during R4.
- Whether the data branch's force-push pattern (planning doc §3.3)
  causes problems when both daily and processor routines push within
  the same hour. Solution if so: rebase-or-retry instead of force-push.
- The exact dashboard polling cadence for follow-up readiness (every
  30s? on user refresh?) — decided in R6.
