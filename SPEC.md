# SPEC.md — v1-routines

The architectural specification for v1-routines lives in
[`docs/routines-version-plan.md`](./docs/routines-version-plan.md).
That document is canonical: every component, decision, and tradeoff
is captured there, and it should be kept up to date as the build
progresses.

This file exists as a pointer because tooling and conventions assume
a `SPEC.md` at the repo root. The actual spec is the planning doc.

---

## Quick map of the planning doc

| Section | Topic |
|---|---|
| §1 | Why this exists; cost driver |
| §2 | Architectural comparison v1 ↔ v1-routines |
| §3 | State management — the data branch |
| §4 | The three routines (daily / follow-up / custom) |
| §5 | Email handling — Resend MCP |
| §6 | Dashboard refactor scope |
| §7 | Phased build plan (mirrored in `BUILD_ORDER.md`) |
| §8 | Open questions (resolved 2026-05-07) |
| §9 | Risks |
| §10 | In-repo vs new repo decision (resolved: new repo) |
| §11 | Next concrete step (which is what got us here) |

## Cross-references

- v1's full SPEC.md (in the v1 repo) remains the source of truth for
  prompt structure, briefing markdown shape, and dashboard UX details
  that v1-routines inherits unchanged.
- v1's V2_VISION.md applies equally here — V2 features remain
  off-limits until Stack opens them.

## Open architectural questions still pending verification in later phases

These are flagged in the planning doc but cannot be resolved until the
relevant phase begins:

1. **Routines API base URL and auth scheme** — confirmed during R1 against the actual `claude routines` CLI behavior
2. **MCP server deploy target** — Cloudflare Workers vs Fly sidecar vs stdio bundled with the routine; decided in R2
3. **Routine concurrency behavior** — what happens if Stack triggers a custom briefing while the daily is mid-run; checked in R3 docs read
4. **Max plan quota consumption per daily run** — measured in R3 once the first real run lands
5. **Session URL exposure on the dashboard** — UX call made during R6
