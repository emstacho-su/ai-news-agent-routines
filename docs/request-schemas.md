# Request schemas — dashboard ↔ processor contract

Locked in Phase R4. The dashboard writes request files into the `data`
branch; the hourly processor routine reads them, writes responses, and
moves processed requests into a `processed/` subfolder. This file is
the contract between R6 (dashboard) and R4 (processor).

---

## Common envelope

Every request file is `<dir>/<id>.json` where `<id>` is a unique,
filesystem-safe identifier the dashboard generates:

```
<unix-millis>-<8-char-random>
e.g. 1747350061234-a3f2c91d
```

ULID would also be fine — the processor doesn't parse `id`, it just
echoes it into the response filename so the dashboard can poll.

---

## Follow-up request

**Path:** `requests/<id>.json`
**Response:** `follow_ups/<id>.md`
**Processed:** `requests/processed/<id>.json` (moved on success)
**Bad:** `requests/bad/<id>.json` (moved if parsing or dispatch fails; see "Failure handling")

```json
{
  "id": "1747350061234-a3f2c91d",
  "kind": "follow_up",
  "created_at": "2026-05-09T14:01:01Z",
  "briefing_date": "2026-05-08",
  "topic_id": "anthropic-spacex-colossus-compute-deal",
  "item_headline": "Anthropic secures all of SpaceX's Colossus 1 compute; rate limits doubled",
  "question": "How does this affect xAI's roadmap?"
}
```

Required fields:
- `id`, `kind`, `created_at`, `question`

At least one of `topic_id` or `item_headline` (preferably both — `topic_id` lets the processor find structured key_facts in `memory.json`; `item_headline` is what the user actually clicked).

`briefing_date` is optional but recommended; it lets the processor pull the full briefing markdown for context.

---

## Custom briefing request

**Path:** `custom_requests/<id>.json`
**Response:** `custom_briefings/<YYYY-MM-DD>_<slug>.md`
  (suffix `_v2`, `_v3`, ... if same date+slug already exists)
**Processed:** `custom_requests/processed/<id>.json`
**Bad:** `custom_requests/bad/<id>.json`

```json
{
  "id": "1747350061234-a3f2c91d",
  "kind": "custom_briefing",
  "created_at": "2026-05-09T14:01:01Z",
  "focus": "Quantum computing for cryptography — recent attack capability",
  "slug": "quantum-cryptography"
}
```

Required: `id`, `kind`, `created_at`, `focus`, `slug`.

`slug` is the dashboard-side kebab-case sanitization of `focus` (max 40
chars, [a-z0-9-]+). Used in the response filename. Dashboard owns the
sanitization to avoid relying on the processor for it.

---

## Response files

### Follow-up response (`follow_ups/<id>.md`)

```markdown
---
request_id: 1747350061234-a3f2c91d
briefing_date: 2026-05-08
topic_id: anthropic-spacex-colossus-compute-deal
answered_at: 2026-05-09T15:02:14Z
---

# Follow-up: Anthropic Colossus deal

**Q:** How does this affect xAI's roadmap?

**A:** xAI is being dissolved as a separate entity and folded into
SpaceX as SpaceXAI; the Colossus compute is now leased to Anthropic.
The roadmap changes are...
```

Frontmatter is YAML and machine-readable; the dashboard's R6 polling
logic uses `request_id` to match. Body is markdown rendered as-is.

### Custom briefing response (`custom_briefings/<date>_<slug>.md`)

Same structure as a daily briefing (TL;DR, sections, sources) but
focused on the request's `focus` text. Sections are flexible per
v1's custom_briefing.txt — Releases, Patterns, Caveats, Practical
Tips for tooling topics; Key Papers / Findings / Open Questions for
research topics.

Frontmatter:
```markdown
---
request_id: 1747350061234-a3f2c91d
focus: "Quantum computing for cryptography — recent attack capability"
slug: quantum-cryptography
generated_at: 2026-05-09T15:02:14Z
---
```

---

## Failure handling

If the processor encounters a request it cannot parse or dispatch
(invalid JSON, missing required field, unknown `kind`):

1. Move the request file to `<dir>/bad/<id>.json` (don't delete — keep
   it for debugging).
2. Append a one-line entry to `<dir>/bad/log.txt`:
   `<id> <ISO timestamp> <one-line reason>`
3. Continue processing other requests in the same run. One bad request
   should not block a valid one in the same poll.

The dashboard treats response-file-missing as "still processing" up to
some timeout (default 2 hours = 2 processor cycles). After the timeout
it surfaces a "may have failed" UI hint and links the user to the
processor routine session URL for debugging.

---

## Empty-poll behavior

When the processor finds no files in `requests/` or `custom_requests/`
(excluding `processed/` and `bad/`), it exits immediately without
committing or pushing anything. Goal: minimize Max-plan token cost on
the 23 hourly runs per day that find nothing.

The processor's prompt should perform the empty check via a single
`ls` or `find` command before any model-driven work begins.
