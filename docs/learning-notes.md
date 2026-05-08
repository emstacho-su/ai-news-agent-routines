# Learning notes — building v1-routines

A retrospective on porting a working hand-rolled agent (v1) onto Claude Code Routines. This file is the long-form companion to the planning doc + deviations log; both of those are reference material, this is opinion.

Written ~24 hours after v1-routines went live. Some of these takes will age poorly as Routines matures.

---

## What I thought I was building vs what I actually built

The planning doc (`docs/routines-version-plan.md`) was written before I had ground-truth knowledge of the Routines API. It assumed:

- Routines exposed an HTTP trigger endpoint the dashboard could POST to and await a response inline.
- Tools were configured as a YAML list with names like `web_search`, `file_read`, `file_write`.
- A custom MCP server (e.g., Resend) could be registered and attached to a routine.
- Quota was loose enough to run an hourly poller.

Reality, learned in order during the build:

- **No HTTP triggers.** Routines fire only on cron (minimum 1-hour interval) or `run_once_at`. The "user clicks button → routine runs → response back" pattern doesn't exist. → Async via a request-file queue on the data branch.
- **Standard Claude Code tool names.** `Bash, Read, Write, Edit, Glob, Grep, WebSearch`. Not the planning doc's `web_search` or `file_read`. → Just write the prompt against the real toolset.
- **Custom MCPs aren't first-class.** Routines accept `mcp_connections` only by `connector_uuid` from claude.ai's pre-registered set (Gmail, Drive, Linear, Microsoft 365, etc.). Arbitrary URL-based MCP servers aren't attachable through the API I had access to. → Drop the Resend MCP, use Gmail. Then drop email entirely (next bullet).
- **Gmail connector is read/draft only.** No `send_email` tool exists in the platform Gmail MCP — only `create_draft`. The "passive morning email" UX was unreachable without building outbound infrastructure. → Email dropped entirely; dashboard becomes the morning read surface.
- **CCR has no git write credentials by default.** First daily routine run produced a perfect briefing in its ephemeral working tree, then 403'd on every push attempt — git push, GitHub Contents API, and the routine's built-in github MCP all failed. → Install the Claude GitHub App on the repo with `Contents: Read+Write`. Once installed, push works without any config change in the routine.
- **Quota is real and small.** A briefing in yesterday's run mentioned ~15 routine runs/day for Max. The hourly processor would consume 24/day on its own — over cap by 10am UTC. → Drop the processor entirely, fold queue draining into the daily routine. One run per day, generates the briefing AND drains up to 5 queued requests in the same session.

Each of these was a 30–60 minute "wait, what?" moment. None of them were in the planning doc because the planning doc was written by an LLM that didn't yet know what the actual API surface looked like.

That's the first lesson, and it's the most important one: **the planning artifact you write before doing the work is wrong about half the things that matter.** This isn't a failure of the planner; it's the shape of building against an API that wasn't in the model's training data. The planning doc's value is the *ordering* and the *trade space* it explores, not the API specifics. Treat your spec as a hypothesis. Keep a deviations log. Update the deviations log every time reality bites.

---

## The architectural simplification I didn't see coming

I went into this build expecting v1-routines to look like v1 with the loop replaced. Same modules, same dashboard, same data files, same email — just the agent.py call replaced with a Routine trigger.

What v1-routines actually became:

- **One routine, fired once a day.** Generates the briefing and drains the request queue in the same session.
- **One git branch, holding all state.** No databases, no Redis, no Fly volumes. Just markdown and JSON files on a force-pushable orphan branch.
- **One serverless function, mostly read.** The dashboard does almost nothing — fetches markdown from raw URLs, writes JSON files to a queue, polls for responses. ~600 lines of Python, no scheduler, no background workers.
- **No email.** The thing that felt mandatory at the start (passive morning notification) turned out to be ceremony when the recipient and sender are the same person. The dashboard is the read surface; the briefing is one click away.

The number of moving parts in v1: agent loop + tool registry + budget tracker + APScheduler + Resend client + memory file + cost tracker + saved-items state + read-flag state + activity SSE + 6 prompts + ~1400-line dashboard + Fly volume.

The number of moving parts in v1-routines: a routine prompt + a git branch + a 600-line dashboard.

I would not have predicted the simplification ratio. The lesson there: **when you move onto managed primitives, you don't just delete the wiring you wrote — you delete a lot of the abstractions you wrote because you needed wiring.** The budget tracker, the activity stream, the in-process scheduler, the SSE plumbing — all of those existed because v1's runtime was hand-rolled and needed observability. Routines is observable through claude.ai's session URLs; budgeting is just the Max plan quota dashboard. Half my code disappeared because the abstractions it was supporting were no longer needed.

---

## Things that were genuinely educational

Specifically the things I'd recommend a similar project tackle, in order:

1. **Build v1 first.** Hand-roll the agent loop, the tool dispatch, the retry, the budget cap. The point isn't that you'll keep this code — the point is that you'll understand what every framework abstraction actually does. When you get to v1-routines and the framework "does memory" for you, you'll know what it's hiding.

2. **Move state to git.** This was a forced move because the routine's environment is ephemeral, but it turned out to be the most pleasant architecture decision. Every state change is a commit. Every commit has a message. The reflog is the audit log. You can `git diff` two days of memory.json to see what stories the agent learned. There's no migration story for the schema because the schema lives in the prompt — when the prompt changes the next run regenerates the data shape. This shouldn't work as well as it does for a single-user system, but it does.

3. **Treat the routine prompt as a source file.** `routines/daily.prompt.md` is in the repo with version control. Changes get reviewed in PRs (well, would be reviewed in PRs if this weren't a one-person project). It's mirrored onto the routine via `RemoteTrigger update`. When the prompt evolves the next run picks it up. The prompt IS the program.

4. **Write tests for the boring parts.** Auth, raw-URL fetch, request-file write, status polling — all 18 tests are small and almost identical in shape (mock GitHub via respx, hit endpoint, assert). They caught two real bugs during the dashboard rewrite. The agent prompt itself isn't tested by pytest — its tests are the manual fires + post-run inspection of what landed on the data branch.

5. **Run the dashboard locally against the live data branch.** This was a happy accident: the dashboard reads from raw GitHub URLs, so local dev sees production data. You don't need a test fixture for "what does a real briefing look like" — you have a real one. Be careful with the write routes in local dev (set `GITHUB_PAT=` to disable them).

---

## Things I'd do differently

- **Verify the API surface before writing the planning doc.** I lost ~2 hours to D1 (Resend MCP), D2 (HTTP triggers), D3 (3 routines vs 1), and D4 (tool names) — all of which were "the planning doc was wrong about how the platform works". A 30-minute exploratory pass with the actual `claude routines` (or in this case, the `RemoteTrigger` MCP tool) before writing the plan would have saved every one of those.

- **Discover quota limits before designing for them.** The hourly processor was a beautiful design until I realized it would burn a day's quota by mid-morning. Stack flagged it. I should have caught it. Quota is a first-class design constraint, not a footnote.

- **Don't build infrastructure I haven't proven I need.** The processor routine got built, tested, deployed, and retired in the same 6-hour window. The retirement was the right call but the build was avoidable — if I'd asked "do I actually need <1hr latency on follow-ups?" before designing the polling architecture, I'd have arrived at "fold drain into the daily routine" directly.

- **Don't over-deliver on the dashboard.** I rewrote `dashboard.py` from 1400 lines to 600, then dropped to a Vercel-compatible subset that's effectively read + queue. The intermediate fully-featured version (saved items, read flags, profile editor) was thrown away when Vercel's filesystem-is-ephemeral nature made local-state writes nonsense. Should have started with the Vercel target in mind.

---

## What's still vestigial

A few things in the repo that aren't load-bearing today and exist as artifacts of the build:

- **`routines/processor.prompt.md`** — the retired hourly processor's prompt. Useful as a reference for what a polling-style design would have looked like; not used at runtime.
- **`profile.py` + `prompts/*.txt`** — carried over from v1. The routine prompt (`routines/daily.prompt.md`) doesn't reference them at runtime. The `prompts/` files live on as the historical record of what v1's prompts looked like.
- **The processor routine on the Anthropic side** — disabled and renamed `(RETIRED — merged into daily)`. Routines API has no delete endpoint; this is the closest equivalent.

I left them in rather than aggressively cleaning up because the audit trail is part of the project's value. A future reader (including me, a year from now) gets to see how the design moved.

---

## What the contrast with v1 actually shows

If you're reading this and trying to decide whether to build the hand-rolled version first or skip straight to managed primitives:

**Hand-rolled first** if:
- You want to deeply understand what an agent loop is doing under the hood.
- You're in a domain where the managed primitives don't exist yet (you're an early adopter on something genuinely new).
- The educational value matters more than shipping speed.

**Managed primitives first** if:
- You just need the product. The agent loop is implementation detail.
- The managed primitive is well-supported in your framework / runtime.
- You don't care about the hand-rolled version as a portfolio piece.

For me (Stack), both were valuable for different reasons. v1 is the artifact of "I built an agent loop." v1-routines is the artifact of "I rebuilt it once I understood what the loop needed to do." Each one's value is partly in the contrast with the other.

If I had to do only one, today, with the knowledge I have now, I would skip v1 and go straight to v1-routines. But I wouldn't have v1's level of confidence about what the routine is doing — and I'd be much more nervous when something broke.

---

## Open threads I haven't resolved

- **Will the 5-request-per-run cap be enough?** If I ever queue 6+ in a day, the 6th waits an extra 24 hours. Hasn't happened in practice but the failure mode is silent. Should probably surface "you have N pending" in the dashboard.

- **What happens when the data branch grows?** Force-push pattern keeps the visible history at one commit, but the reflog accumulates. Long-term: `git gc --aggressive` periodically, or rotate the data branch (data-2026-q3, data-2026-q4) and make the dashboard look at the latest.

- **What happens when the Routines product changes?** The daily routine has been live for two days as I write this. The API I'm calling via `RemoteTrigger` is documented as a research preview. If the surface changes I'll need to update the routine prompts and possibly the dashboard's request shape. The deviations doc would absorb the diff.

- **Is the Max plan quota actually 15/day?** Came from a single source in yesterday's briefing. I assumed conservatively (one routine, drain inline). If it's higher I have headroom; if lower the design holds. Worth verifying when Anthropic publishes the number authoritatively.

These aren't urgent. They're the next conversation a thoughtful reviewer would want to have.
