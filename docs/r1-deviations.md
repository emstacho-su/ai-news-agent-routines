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

### D1. Email path removed entirely (Resend MCP and Gmail both eliminated)

**Planned (§5):** Build a custom Resend MCP server (TypeScript), register
it, attach it to the daily routine.

**First correction (R1):** Use the **Gmail connector** instead — no
custom MCP needed.

**Second correction (R3, after first real run):** Drop email entirely.
Per G2.5, the Gmail connector exposes `create_draft` only — no
`send_email`. So the only achievable "email" path was a Gmail draft
that Stack would open and manually send to himself. Stack's correct
observation: if the recipient is also the sender, the draft is just
the briefing — there's no point in the email round-trip. The dashboard
becomes the read surface.

**Effect on build plan:**
- Phase **R2 is deleted entirely** (was true after R1; even more true now).
- The daily routine's prompt has no email step (R3 prompt revision).
- The dashboard (R6 + R7) is the morning read surface.
- A future browser-push notification (R6 or R9 polish) signals when a
  new briefing lands on the data branch, replacing the email's
  attention-grab function.

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

### D3. Routine count changed from 3 to 1 (final)

**Planned (§4):** three routines — daily, follow-up, custom.

**First correction (R1):** **two routines** — daily + combined hourly processor.

**Second correction (post-R7, surfaced by Stack):** **one routine**.
The Max plan ships ~15 routine runs/day. An hourly processor would
consume 24/day on its own, blowing the cap by ~10am UTC. Even at every
3 hours that's 8 runs/day plus 1 daily = 9 runs leaving 6 for manual
fires — workable but anxious.

The cleaner design Stack pointed to: **fold queue draining into the
daily routine's prompt**. The daily routine already runs once per day
at 12:00 UTC, generates the briefing, updates memory, and pushes. We
extended its prompt with a Step 5.5 that, before the final commit,
checks `requests/` and `custom_requests/` and processes up to 5
queued items in the same run. One commit covers the briefing + the
drained responses.

Trade: follow-up + custom briefings now wait until the next morning's
daily run (up to 24hr latency) rather than the next hourly tick (up
to 1hr). For Stack's actual usage — daily reading, occasional
follow-ups — that latency is acceptable. In exchange we drop from
25 routine runs/day (26 with the daily) to **1**, leaving 14 in
reserve for manual fires + safety margin.

**Effect on build plan:**
- Daily routine prompt (`routines/daily.prompt.md`) gained Step 5.5
  with a 5-request cap and an 8-WebSearch drain budget on top of the
  daily briefing's existing budget. Combined caps: 45 tool calls /
  20 WebSearches per run.
- Processor routine (`trig_016P6y3fNZp3utmDduv5tA6D`) was renamed to
  `ai-news-agent-processor (RETIRED — merged into daily)` and set
  to `enabled: false`. The Routines API doesn't expose deletion;
  retiring + disabling is the closest we get.
- Dashboard JS messaging updated: "next processor run within 1hr" →
  "drained at next 12:00 UTC daily run".
- Polling cadence on the dashboard relaxed from 30s to 5min (24hr
  worst-case wait makes 30s polling silly).
- `routines/processor.prompt.md` kept in repo as a reference for the
  retired design.

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

### G2.5. Email auto-send is not available via the platform connectors

The Gmail connector (and Microsoft 365 / Outlook) only expose
read/search/draft tools. There is no `send_email` or `send_draft` tool.
Routines can create drafts, but a human must open the inbox and click
send. The constraint is platform-wide and looks intentional
(human-in-the-loop on outbound communication).

For v1-routines this kills the "passive morning email" UX. Verified
during the R3 first run on 2026-05-08.

**Effect on architecture:** the daily routine drops the email step
entirely. The dashboard is the read surface — Stack opens it each
morning. A future enhancement (R6 or R9 polish) is a browser push
notification when a new briefing lands on the data branch.

### G2.7. Routine environment has no git write credentials by default

First R3 run failed both `git push origin data` AND
`gh api PUT contents/...` AND the built-in `github.push_files` MCP tool —
all 403. Conclusion: the CCR session is authenticated for
**read** (cloning works fine) but not write to repos owned by the user
unless the **Claude GitHub App** is explicitly installed on the repo
with **Contents: Read and write** permission.

**Effect on R3:** Stack must install the Claude GitHub App on
`emstacho-su/ai-news-agent-routines` before R3 can pass verification.
Once installed, the routine's `git push origin data` (or the
GitHub-MCP `push_files` fallback) should succeed without any change to
the routine config.

### G2.9. `mcp_connections: []` on update is a no-op

Passing `mcp_connections: []` in an update body is treated as "no
change" rather than "remove all connectors". To remove all attached
connectors during an update, pass `clear_mcp_connections: true`
instead. To replace with a specific subset, pass
`mcp_connections: [{...}]` with at least one entry.

This is symmetric with G1 (omit on create = auto-attach all) but the
remediation is different (`clear_mcp_connections: true` only works on
update, not create).

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
