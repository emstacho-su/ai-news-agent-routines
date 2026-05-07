"""FastAPI dashboard for the AI News Agent.

Phase 5 MVP per SPEC section 12:

  - Single-user HTTP basic auth (DASHBOARD_PASSWORD env var)
  - GET /                       List of daily briefings (HTML)
  - GET /briefing/{date}        Render one briefing (markdown -> HTML)
  - POST /trigger/daily         Kick off a daily run; returns job_id
  - GET /status/{job_id}        Poll a running job
  - GET /api/budget             Current spend / cap / ratio (JSON)
  - GET /static/*               style.css, app.js, icons

Async job execution: trigger returns immediately with a job_id; the
agent runs in a background asyncio task wrapped around asyncio.to_thread
(run_agent is synchronous). Job state lives in an in-memory dict, lost
on process restart -- acceptable for v1 per SPEC section 12.

Phase 6 will add an SSE endpoint for real-time activity streaming;
Phase 7 adds profile + saved items + read state; Phase 9 adds
follow-up Q&A. Each is layered on top of this scaffold.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import markdown as markdown_lib
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

import budget
import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_security = HTTPBasic()


def _expected_password() -> str:
    """Read DASHBOARD_PASSWORD lazily so tests can monkeypatch os.environ."""
    return os.environ.get("DASHBOARD_PASSWORD", "")


def require_auth(
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    """Constant-time password comparison; rejects empty passwords too."""
    expected = _expected_password()
    if not expected:
        # Fail closed if the operator forgot to set the password.
        logger.error("DASHBOARD_PASSWORD is not set; rejecting all requests")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard password not configured.",
        )
    correct = secrets.compare_digest(
        credentials.password.encode("utf-8"), expected.encode("utf-8")
    )
    if not correct:
        logger.warning("auth failure for username=%r", credentials.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Job tracking (in-memory; lost on restart)
# ---------------------------------------------------------------------------
JOBS: dict[str, dict] = {}


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _record_job(job_id: str, **fields: Any) -> dict:
    JOBS.setdefault(job_id, {"job_id": job_id})
    JOBS[job_id].update(fields)
    return JOBS[job_id]


def _truncate_for_event(value: Any, limit: int = 240) -> Any:
    """Reduce arbitrarily large args dicts to something safe to push over SSE."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _make_stream_callback(job_id: str):
    """Return a callback that records (message, args) into JOBS[job_id].events.

    The agent loop calls this synchronously from its own thread; we just
    append to a list, which is GIL-safe in CPython for small ops. The SSE
    endpoint reads the same list cooperatively.
    """
    def cb(message: str, args: dict) -> None:
        job = JOBS.get(job_id)
        if job is None:
            return
        events = job.setdefault("events", [])
        events.append(
            {
                "ts": _now_iso(),
                "message": str(message),
                "args": _truncate_for_event(args),
            }
        )
    return cb


async def _run_custom_job(job_id: str, focus: str) -> None:
    """Background runner for Phase 10 custom briefings.

    Mirrors :func:`_run_daily_job` but uses the custom prompt + tools and
    threads ``focus`` into the initial message verbatim so the model
    can echo it back via ``finalize_custom_briefing``.
    """
    import profile
    from agent import run_agent
    from tools import CUSTOM_TOOLS, dispatch_tool, last_finalize_result

    _record_job(
        job_id,
        status="running",
        started_at=_now_iso(),
        events=[],
        focus=focus,
        run_type="custom",
    )

    base_prompt = (config.PROMPTS_DIR / "custom_briefing.txt").read_text(
        encoding="utf-8"
    )
    system_prompt = profile.system_prompt_with_profile(base_prompt)
    today = datetime.now(timezone.utc).date()
    initial_message = (
        f"Today is {today.isoformat()}. Focus area: {focus!s}\n\n"
        "Produce a focused custom briefing on this topic following the "
        "process and structure in your system prompt. Pass the focus "
        "text verbatim to finalize_custom_briefing when ready."
    )

    try:
        result = await asyncio.to_thread(
            run_agent,
            system_prompt=system_prompt,
            initial_message=initial_message,
            tools=CUSTOM_TOOLS,
            max_iterations=config.DAILY_MAX_ITERATIONS,
            max_tool_calls=config.DAILY_MAX_TOOL_CALLS,
            dispatch_tool=dispatch_tool,
            stream_callback=_make_stream_callback(job_id),
            run_type="custom",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("custom job %s failed", job_id)
        _record_job(
            job_id,
            status="error",
            ended_at=_now_iso(),
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    finalize = last_finalize_result()
    summary = {
        "iterations": result.iterations,
        "tool_calls": result.tool_calls,
        "usage": result.usage,
        "focus": focus,
    }
    if finalize is not None:
        summary["briefing_path"] = str(
            finalize.path.relative_to(config.PROJECT_ROOT)
        )
        summary["filename"] = finalize.path.name
        summary["word_count"] = finalize.word_count

    _record_job(job_id, status="complete", ended_at=_now_iso(), result=summary)


async def _run_daily_job(job_id: str, *, scheduled: bool = False) -> None:
    """Background runner: invokes the synchronous run_agent in a thread.

    ``scheduled`` flips the email-notification path on (SPEC §13). Manual
    triggers from the dashboard never fire emails -- the user is already
    on the dashboard if they clicked the button.
    """
    # Lazy imports so dashboard module load doesn't require anthropic
    # to be installed (e.g. during static-only tests).
    import profile
    from agent import run_agent
    from tools import DAILY_TOOLS, dispatch_tool, last_finalize_result

    _record_job(job_id, status="running", started_at=_now_iso(), events=[])

    base_prompt = (config.PROMPTS_DIR / "daily_briefing.txt").read_text(
        encoding="utf-8"
    )
    # Phase 7: Stack's profile.md is prepended at run time so the agent
    # picks up edits made via the dashboard's /profile page.
    system_prompt = profile.system_prompt_with_profile(base_prompt)
    today = datetime.now(timezone.utc).date()
    initial_message = (
        f"Today is {today.isoformat()}. Produce today's AI news briefing "
        "following the process and structure in your system prompt. Call "
        "finalize_briefing exactly once when ready."
    )

    try:
        result = await asyncio.to_thread(
            run_agent,
            system_prompt=system_prompt,
            initial_message=initial_message,
            tools=DAILY_TOOLS,
            max_iterations=config.DAILY_MAX_ITERATIONS,
            max_tool_calls=config.DAILY_MAX_TOOL_CALLS,
            dispatch_tool=dispatch_tool,
            stream_callback=_make_stream_callback(job_id),
            run_type="daily",
        )
    except Exception as exc:  # noqa: BLE001 — surface ALL errors to the UI
        logger.exception("daily job %s failed", job_id)
        err_msg = f"{type(exc).__name__}: {exc}"
        _record_job(
            job_id,
            status="error",
            ended_at=_now_iso(),
            error=err_msg,
        )
        if scheduled:
            await _notify_failure(job_id, err_msg)
        return

    finalize = last_finalize_result()
    summary = {
        "iterations": result.iterations,
        "tool_calls": result.tool_calls,
        "usage": result.usage,
    }
    if finalize is not None:
        try:
            summary["briefing_path"] = str(
                finalize.path.relative_to(config.PROJECT_ROOT)
            )
        except ValueError:
            summary["briefing_path"] = str(finalize.path)
        summary["word_count"] = finalize.word_count
        summary["filename"] = finalize.path.name

    _record_job(job_id, status="complete", ended_at=_now_iso(), result=summary)

    if scheduled and finalize is not None:
        await _notify_briefing_ready(job_id, finalize.path)


async def _notify_briefing_ready(job_id: str, briefing_path) -> None:
    """Phase 11: send the success email after a scheduled run.

    Errors are swallowed -- a failed email must not break the job
    record. The notification result lands on the job dict so the
    dashboard / logs can surface it.
    """
    import notifications

    filename = briefing_path.name
    # The filename is either {date}.md or {date}_v{n}.md; extract date.
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", filename)
    date_str = m.group(1) if m else _now_iso()[:10]
    try:
        markdown_text = briefing_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not re-read briefing for email: %s", exc)
        markdown_text = ""
    notify_result = await asyncio.to_thread(
        notifications.send_briefing_email,
        date_str,
        briefing_markdown=markdown_text,
        filename=filename,
    )
    job = JOBS.get(job_id)
    if job is not None:
        job.setdefault("result", {})["notification"] = notify_result


async def _notify_failure(job_id: str, error_msg: str) -> None:
    """Phase 11: send the failure email after a scheduled run errors."""
    import notifications

    today = datetime.now(timezone.utc).date().isoformat()
    notify_result = await asyncio.to_thread(
        notifications.send_failure_email,
        today,
        error=error_msg,
        job_id=job_id,
    )
    job = JOBS.get(job_id)
    if job is not None:
        job["notification"] = notify_result


# ---------------------------------------------------------------------------
# HTML templating (placeholder substitution -- no Jinja2 dep)
# ---------------------------------------------------------------------------
_BRIEFING_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_v(\d+))?\.md$")
# Custom filename: {date}_{slug}[_vN].md. Slug is lowercase alphanum/dash
# only (see tools.slugify_focus). The regex doubles as the path-traversal
# guard for /custom/{filename}.
_CUSTOM_FILENAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_([a-z0-9][a-z0-9-]{0,79})(?:_v\d+)?\.md$"
)


def _list_briefings() -> list[dict]:
    """Return briefings sorted newest first, including same-day rerun
    editions. Each entry carries a ``version`` integer (1 for the
    canonical ``{date}.md``, 2+ for ``{date}_v{n}.md``) and a
    ``filename`` so the listing can link straight to the right edition.
    """
    out: list[dict] = []
    if not config.BRIEFINGS_DIR.exists():
        return out
    for path in sorted(config.BRIEFINGS_DIR.glob("*.md"), reverse=True):
        m = _BRIEFING_FILENAME_RE.match(path.name)
        if not m:
            continue
        date_str = m.group(1)
        version = int(m.group(2)) if m.group(2) else 1
        first_line = ""
        try:
            with path.open(encoding="utf-8") as fh:
                first_line = fh.readline().lstrip("#").strip()
        except OSError:
            pass
        out.append(
            {
                "date": date_str,
                "version": version,
                "filename": path.name,
                "title": first_line or f"Briefing for {date_str}",
                "size_bytes": path.stat().st_size,
            }
        )
    # Sort by (date desc, version desc) so same-day rerun editions land
    # together newest-first within the date.
    out.sort(key=lambda b: (b["date"], b["version"]), reverse=True)
    return out


def _list_custom_briefings() -> list[dict]:
    """Return custom briefings sorted newest first.

    Each entry includes the filename (so it round-trips into the URL),
    the date prefix, the slug-derived focus, and the rendered first
    line as a title. Files that don't match the canonical
    ``{date}_{slug}[_vN].md`` shape are ignored so a stray hand-edited
    file can't break the listing or the view route.
    """
    out: list[dict] = []
    if not config.CUSTOM_BRIEFINGS_DIR.exists():
        return out
    for path in sorted(
        config.CUSTOM_BRIEFINGS_DIR.glob("*.md"), reverse=True
    ):
        m = _CUSTOM_FILENAME_RE.match(path.name)
        if not m:
            continue
        date_str = m.group(1)
        slug = m.group(2)
        first_line = ""
        try:
            with path.open(encoding="utf-8") as fh:
                first_line = fh.readline().lstrip("#").strip()
        except OSError:
            pass
        out.append(
            {
                "filename": path.name,
                "date": date_str,
                "slug": slug,
                "title": first_line or path.stem.replace("_", " "),
                "size_bytes": path.stat().st_size,
            }
        )
    return out


def _render_custom_list_html(briefings: list[dict]) -> str:
    if not briefings:
        return '<p class="empty">No custom briefings yet. Trigger one above.</p>'
    items = []
    for b in briefings:
        items.append(
            f'<li><a href="/custom/{_html_escape(b["filename"])}">'
            f'<span class="date">{_html_escape(b["date"])}</span>'
            f'<span class="title">{_html_escape(b["title"])}</span>'
            f'</a></li>'
        )
    return '<ul class="briefings">' + "".join(items) + "</ul>"


def _briefing_href(b: dict) -> str:
    """Build the dashboard URL for a briefing entry. v1 uses the bare
    /briefing/{date} URL so existing bookmarks keep working; v2+ goes
    through /briefing/{date}/v{n}."""
    if b.get("version", 1) == 1:
        return f'/briefing/{b["date"]}'
    return f'/briefing/{b["date"]}/v{b["version"]}'


def _render_briefings_list_html(
    briefings: list[dict],
    *,
    empty_message: str = "No briefings yet. Trigger one above.",
) -> str:
    if not briefings:
        return f'<p class="empty">{_html_escape(empty_message)}</p>'
    items = []
    for b in briefings:
        snippet_html = ""
        snippet = b.get("snippet")
        if snippet:
            snippet_html = (
                f'<span class="snippet">{_html_escape(snippet)}</span>'
            )
        version_html = ""
        version = b.get("version", 1)
        if version > 1:
            version_html = f'<span class="version-tag">v{version}</span>'
        items.append(
            f'<li><a href="{_briefing_href(b)}">'
            f'<span class="date">{b["date"]}</span>'
            f'<span class="title">{_html_escape(b["title"])}{version_html}</span>'
            f"{snippet_html}"
            f"</a></li>"
        )
    return '<ul class="briefings">' + "".join(items) + "</ul>"


# ---------------------------------------------------------------------------
# Filtering and search (Phase 8)
# ---------------------------------------------------------------------------
# Phase 2's renderer slugs the section names ("Tools & Frameworks" -> "tools").
# When the user filters by category=tools, we look for the section heading
# in the briefing body. Reverse-lookup table keeps the canonical names in
# one place so the filter and the renderer agree on the vocabulary.
_SLUG_TO_SECTION_NAME: dict[str, str] = {
    "anthropic": "Anthropic",
    "tools": "Tools & Frameworks",
    "models": "Model Releases",
    "research": "Research & Technical",
    "wildcard": "Wildcard",
    "practical": "Practical Tip of the Day",
}


def _slug_to_section_name(slug: str) -> str:
    return _SLUG_TO_SECTION_NAME.get((slug or "").strip().lower(), slug)


def _extract_snippet(content: str, q: str, max_len: int = 220) -> str:
    """Return ~max_len characters of context around the first match.

    Whitespace is collapsed so the snippet renders as a single line in
    the briefings list.
    """
    if not q or not content:
        return ""
    lower = content.lower()
    needle = q.lower()
    idx = lower.find(needle)
    if idx == -1:
        return ""
    half = max(1, max_len // 2)
    start = max(0, idx - half)
    end = min(len(content), idx + len(q) + half)
    snippet = content[start:end]
    # Collapse internal whitespace
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet


def _filter_briefings(
    briefings: list[dict],
    *,
    q: str = "",
    since: str = "",
    until: str = "",
    category: str = "",
) -> list[dict]:
    """Apply all four filter dimensions to a briefings list.

    Filters are AND-combined. Each briefing's content is read at most
    once per call. Snippets are attached when `q` is provided.
    """
    needs_content = bool(q or category)
    out: list[dict] = []
    for b in briefings:
        date_str = b.get("date", "")
        if since and date_str < since:
            continue
        if until and date_str > until:
            continue

        if not needs_content:
            out.append(b)
            continue

        path = config.BRIEFINGS_DIR / f"{date_str}.md"
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue

        if category:
            section = _slug_to_section_name(category)
            # Match "## Anthropic" anchored at line start
            if f"\n## {section}" not in content and not content.startswith(
                f"## {section}"
            ):
                continue

        if q:
            if q.lower() not in content.lower():
                continue
            b = dict(b)
            b["snippet"] = _extract_snippet(content, q)

        out.append(b)
    return out


def _memory_tag_counts() -> dict[str, int]:
    """Return {category: count} from memory.json. Empty dict if no memory."""
    try:
        import memory as _memory
    except ImportError:
        return {}
    try:
        store = _memory.load_memory()
    except Exception:  # noqa: BLE001 — corrupt memory shouldn't break the home page
        return {}
    counts: dict[str, int] = {}
    for topic in store.get("topics") or []:
        cat = (topic.get("category") or "other").strip().lower()
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    return counts


def _render_tag_links_html(
    counts: dict[str, int], active_category: str = ""
) -> str:
    """Render the memory-categories sidebar as filter pill links."""
    if not counts:
        return ""
    active = (active_category or "").strip().lower()
    pills = []
    for slug in sorted(counts):
        label = _slug_to_section_name(slug)
        n = counts[slug]
        cls = "tag-pill active" if slug == active else "tag-pill"
        pills.append(
            f'<a class="{cls}" href="/?category={slug}">'
            f'{_html_escape(label)} <span class="count">{n}</span></a>'
        )
    return '<div class="tag-pills">' + "".join(pills) + "</div>"


def _render_budget_banner_html() -> str:
    """Phase 12: warn at 80% of monthly cap, hard-stop at 100%.

    Renders nothing below 80%. Above 100% the banner reads "BUDGET CAP
    REACHED" so manual triggers (which 402 anyway) get a visual cue
    before they click."""
    try:
        status_doc = budget.current_status()
    except Exception:  # noqa: BLE001 — never let bookkeeping break the page
        return ""
    ratio = float(status_doc.get("ratio") or 0.0)
    if ratio < config.BUDGET_WARN_THRESHOLD:
        return ""
    spent = float(status_doc.get("month_usd") or 0.0)
    cap = float(status_doc.get("cap_usd") or 0.0)
    pct = int(ratio * 100)
    if ratio >= 1.0:
        text = (
            f"BUDGET CAP REACHED — ${spent:.2f} / ${cap:.0f} ({pct}%). "
            "Manual triggers will refuse until the next monthly reset."
        )
        cls = "budget-banner danger"
    else:
        text = (
            f"Budget at {pct}% — ${spent:.2f} of ${cap:.0f} this month. "
            "Manual triggers still allowed; further reruns will eat into headroom."
        )
        cls = "budget-banner warn"
    return f'<div class="{cls}" role="status">{_html_escape(text)}</div>'


def _render_category_select_html(active_category: str = "") -> str:
    """Build the <select> for the home filter form with the right option
    preselected. Server-rendered so the page works without JS."""
    options = [
        ("", "all sections"),
        ("anthropic", "Anthropic"),
        ("tools", "Tools & Frameworks"),
        ("models", "Model Releases"),
        ("research", "Research & Technical"),
        ("wildcard", "Wildcard"),
    ]
    active = (active_category or "").strip().lower()
    rendered = []
    for value, label in options:
        sel = ' selected="selected"' if value == active else ""
        rendered.append(
            f'<option value="{value}"{sel}>{_html_escape(label)}</option>'
        )
    return (
        '<select name="category" class="filter-input">'
        + "".join(rendered)
        + "</select>"
    )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_template(name: str, **substitutions: str) -> str:
    """Read a static template and substitute {{placeholder}} markers."""
    template = (config.PROJECT_ROOT / "static" / name).read_text(
        encoding="utf-8"
    )
    out = template
    for key, value in substitutions.items():
        out = out.replace("{{" + key + "}}", value)
    return out


# ---------------------------------------------------------------------------
# Personalization state (Phase 7)
# ---------------------------------------------------------------------------
# Three small JSON files in data/. Saved items + read flags are user
# state surfaced by the dashboard. Engagement is captured for later
# (v2) but never read by the v1 agent.

_ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_item_key(date: str, item_id: str) -> tuple[str, str]:
    if not _DATE_RE.match(date):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"date must be YYYY-MM-DD, got {date!r}",
        )
    if not _ITEM_ID_RE.match(item_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "item_id must be lowercase alphanumeric/dash, "
                f"got {item_id!r}"
            ),
        )
    return date, item_id


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        import json as _json
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("state file %s is unreadable; returning default", path)
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _load_saved_items() -> list[dict]:
    return _load_json(config.SAVED_ITEMS_FILE, [])


def _load_read_state() -> dict:
    return _load_json(config.DASHBOARD_STATE_FILE, {"read": {}})


def _load_engagement() -> dict:
    return _load_json(config.ENGAGEMENT_FILE, {"clicks": []})


def toggle_saved(date: str, item_id: str) -> dict:
    """Toggle a (date, item_id) entry on/off in saved_items.json."""
    items = _load_saved_items()
    key = f"{date}:{item_id}"
    existing = next(
        (i for i in items if f"{i.get('date')}:{i.get('item_id')}" == key),
        None,
    )
    if existing:
        items = [i for i in items if i is not existing]
        is_saved = False
    else:
        items.append(
            {"date": date, "item_id": item_id, "saved_at": _now_iso()}
        )
        is_saved = True
    _save_json(config.SAVED_ITEMS_FILE, items)
    return {"saved": is_saved, "date": date, "item_id": item_id}


def toggle_read(date: str, item_id: str) -> dict:
    """Toggle a (date, item_id) entry on/off in dashboard_state.json."""
    state = _load_read_state()
    read_map = state.setdefault("read", {})
    key = f"{date}:{item_id}"
    if read_map.get(key):
        del read_map[key]
        is_read = False
    else:
        read_map[key] = _now_iso()
        is_read = True
    _save_json(config.DASHBOARD_STATE_FILE, state)
    return {"read": is_read, "date": date, "item_id": item_id}


def record_engagement(date: str, item_id: str, kind: str) -> dict:
    """Append a click/view event to engagement.json. Capped to most-recent
    5000 entries so the file doesn't grow unbounded."""
    eng = _load_engagement()
    clicks = eng.setdefault("clicks", [])
    clicks.append(
        {"date": date, "item_id": item_id, "kind": kind, "ts": _now_iso()}
    )
    if len(clicks) > 5000:
        eng["clicks"] = clicks[-5000:]
    _save_json(config.ENGAGEMENT_FILE, eng)
    return {"recorded": True, "kind": kind}


# ---------------------------------------------------------------------------
# Scheduler integration (SPEC §14)
# ---------------------------------------------------------------------------
async def _scheduled_daily_tick() -> None:
    """Callable APScheduler invokes at DAILY_CRON_UTC.

    Reuses ``_run_daily_job`` so the agent run, briefing write, email,
    and budget bookkeeping all flow through the same path the dashboard
    button uses. ``scheduled=True`` flips on the email path.
    """
    job_id = _new_job_id()
    logger.info("scheduled daily tick firing as job_id=%s", job_id)
    await _run_daily_job(job_id, scheduled=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start the in-process scheduler on app startup; stop it on shutdown.

    Tests run with ``SCHEDULER_ENABLED=0`` so the import of dashboard
    does not start a real background timer.
    """
    if config.SCHEDULER_ENABLED:
        # Lazy import so test environments that lack APScheduler still
        # load the dashboard module cleanly.
        import scheduler

        scheduler.start_scheduler(_scheduled_daily_tick)
    try:
        yield
    finally:
        if config.SCHEDULER_ENABLED:
            import scheduler

            scheduler.shutdown_scheduler()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    app = FastAPI(
        title="AI News Agent",
        description="Personal autonomous AI news briefing dashboard.",
        version=config.VERSION,
        lifespan=_lifespan,
    )

    static_dir = config.PROJECT_ROOT / "static"
    if static_dir.exists():
        app.mount(
            "/static", StaticFiles(directory=str(static_dir)), name="static"
        )

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def home(
        q: str = "",
        since: str = "",
        until: str = "",
        category: str = "",
        _user: str = Depends(require_auth),
    ) -> HTMLResponse:
        all_briefings = _list_briefings()

        q = (q or "").strip()
        since = (since or "").strip()
        until = (until or "").strip()
        category = (category or "").strip().lower()
        # Silently ignore malformed dates so a junk URL param doesn't 500
        if since and not _DATE_RE.match(since):
            since = ""
        if until and not _DATE_RE.match(until):
            until = ""

        filters_active = bool(q or since or until or category)
        if filters_active:
            filtered = _filter_briefings(
                all_briefings,
                q=q,
                since=since,
                until=until,
                category=category,
            )
            empty_msg = "No briefings match the current filters."
        else:
            filtered = all_briefings
            empty_msg = "No briefings yet. Trigger one above."

        tag_counts = _memory_tag_counts()
        budget_banner = _render_budget_banner_html()
        body = _render_template(
            "index.html",
            briefings_list=_render_briefings_list_html(
                filtered, empty_message=empty_msg
            ),
            briefing_count=str(len(filtered)),
            total_briefings=str(len(all_briefings)),
            search_value=_html_escape(q),
            since_value=_html_escape(since),
            until_value=_html_escape(until),
            category_value=_html_escape(category),
            category_select=_render_category_select_html(category),
            tag_links=_render_tag_links_html(tag_counts, active_category=category),
            budget_banner=budget_banner,
        )
        return HTMLResponse(body)

    def _serve_briefing(filename: str, header_label: str) -> HTMLResponse:
        """Shared body for the /briefing/{date}[/v{n}] view routes."""
        path = config.BRIEFINGS_DIR / filename
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No briefing at {filename}",
            )
        markdown_text = path.read_text(encoding="utf-8")
        html = markdown_lib.markdown(
            markdown_text,
            extensions=["fenced_code", "tables", "md_in_html"],
        )
        body = _render_template(
            "briefing.html",
            briefing_date=_html_escape(header_label),
            briefing_html=html,
        )
        return HTMLResponse(body)

    @app.get("/briefing/{date}", response_class=HTMLResponse)
    async def view_briefing(
        date: str, _user: str = Depends(require_auth)
    ) -> HTMLResponse:
        if not _DATE_RE.match(date):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="date must be YYYY-MM-DD",
            )
        return _serve_briefing(f"{date}.md", date)

    @app.get("/briefing/{date}/v{version}", response_class=HTMLResponse)
    async def view_briefing_version(
        date: str, version: int, _user: str = Depends(require_auth)
    ) -> HTMLResponse:
        """Phase 12: same-day rerun editions live at /briefing/{date}/v{n}.
        v1 is served from the canonical /briefing/{date} URL."""
        if not _DATE_RE.match(date):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="date must be YYYY-MM-DD",
            )
        if version < 1 or version > 99:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="version must be 1-99",
            )
        if version == 1:
            # Canonical edition lives without the suffix.
            return _serve_briefing(f"{date}.md", date)
        return _serve_briefing(f"{date}_v{version}.md", f"{date} v{version}")

    @app.post("/trigger/daily")
    async def trigger_daily(
        _user: str = Depends(require_auth),
    ) -> dict:
        # Refuse to start if budget is already over the cap. The dashboard
        # has no way to send --override-budget; that's CLI-only by design
        # (Phase 4 spec: emergency override).
        try:
            budget.check_budget()
        except budget.BudgetExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=str(exc),
            )
        job_id = _new_job_id()
        _record_job(job_id, status="queued", started_at=_now_iso())
        # Fire-and-forget background task
        asyncio.create_task(_run_daily_job(job_id))
        return {"job_id": job_id, "status": "queued"}

    @app.get("/custom", response_class=HTMLResponse)
    async def list_custom(_user: str = Depends(require_auth)) -> HTMLResponse:
        items = _list_custom_briefings()
        body = _render_template(
            "custom.html",
            custom_list=_render_custom_list_html(items),
            custom_count=str(len(items)),
        )
        return HTMLResponse(body)

    @app.get("/custom/{filename}", response_class=HTMLResponse)
    async def view_custom(
        filename: str, _user: str = Depends(require_auth)
    ) -> HTMLResponse:
        # The regex is also the path-traversal guard: it forbids slashes
        # and '..' implicitly via the character class.
        if not _CUSTOM_FILENAME_RE.match(filename):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="filename must match {date}_{slug}[_vN].md",
            )
        path = config.CUSTOM_BRIEFINGS_DIR / filename
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No custom briefing at {filename}",
            )
        markdown_text = path.read_text(encoding="utf-8")
        html = markdown_lib.markdown(
            markdown_text,
            extensions=["fenced_code", "tables", "md_in_html"],
        )
        # The briefing.html template's {{briefing_date}} placeholder is
        # used here as a generic header label; we feed it the filename.
        body = _render_template(
            "briefing.html",
            briefing_date=_html_escape(filename),
            briefing_html=html,
        )
        return HTMLResponse(body)

    @app.post("/trigger/custom")
    async def trigger_custom(
        request: Request, _user: str = Depends(require_auth)
    ) -> dict:
        try:
            budget.check_budget()
        except budget.BudgetExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=str(exc),
            )
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="body must be JSON {focus: ...}",
            )
        focus = str(body.get("focus") or "").strip()
        if not focus:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="focus must not be empty",
            )
        if len(focus) > 500:
            raise HTTPException(
                status_code=413,
                detail="focus exceeds 500 chars",
            )
        job_id = _new_job_id()
        _record_job(
            job_id, status="queued", started_at=_now_iso(), focus=focus,
            run_type="custom",
        )
        asyncio.create_task(_run_custom_job(job_id, focus))
        return {"job_id": job_id, "status": "queued", "focus": focus}

    @app.get("/status/{job_id}")
    async def job_status(
        job_id: str, _user: str = Depends(require_auth)
    ) -> dict:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown job_id {job_id!r}",
            )
        return job

    @app.get("/status/{job_id}/stream")
    async def job_status_stream(
        job_id: str, _user: str = Depends(require_auth)
    ):
        """SSE: yield activity events as they're appended, then a final
        `done` event when the job reaches a terminal state.

        The browser's EventSource keeps the connection open; we poll the
        in-memory event list every ~300ms because the agent loop runs in
        a separate thread and pushing across threads cleanly would
        require asyncio.Queue + run_coroutine_threadsafe ceremony.
        Polling a Python list is GIL-safe and good enough for v1's
        single-user single-job profile.
        """
        if JOBS.get(job_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown job_id {job_id!r}",
            )

        async def event_gen():
            last_idx = 0
            while True:
                job = JOBS.get(job_id)
                if job is None:
                    yield 'data: {"type":"done","status":"missing"}\n\n'
                    return

                events = job.get("events") or []
                while last_idx < len(events):
                    payload = json.dumps(events[last_idx])
                    yield f"data: {payload}\n\n"
                    last_idx += 1

                current = job.get("status")
                if current in ("complete", "error"):
                    final = {
                        "type": "done",
                        "status": current,
                        "result": job.get("result"),
                        "error": job.get("error"),
                    }
                    yield f"data: {json.dumps(final)}\n\n"
                    return

                await asyncio.sleep(0.3)

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                # Disable proxy buffering so events flush immediately.
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/budget")
    async def api_budget(_user: str = Depends(require_auth)) -> dict:
        return budget.current_status()

    # ---------------- Phase 7: profile / saved / read / engagement ----------
    @app.get("/profile", response_class=HTMLResponse)
    async def get_profile(_user: str = Depends(require_auth)) -> HTMLResponse:
        import profile as profile_mod

        body = _render_template(
            "profile.html",
            profile_content=_html_escape(profile_mod.load_profile()),
        )
        return HTMLResponse(body)

    @app.post("/profile")
    async def post_profile(
        request: Request, _user: str = Depends(require_auth)
    ) -> dict:
        # Accepts JSON {"content": "<markdown>"}. We avoid form parsing
        # to skip the python-multipart dependency.
        import profile as profile_mod

        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="body must be JSON {content: ...}",
            )
        new_content = str(body.get("content") or "").strip()
        if not new_content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="profile content must not be empty",
            )
        if len(new_content) > 50_000:
            # FastAPI deprecated HTTP_413_REQUEST_ENTITY_TOO_LARGE in favor
            # of HTTP_413_CONTENT_TOO_LARGE; both yield the same status code.
            raise HTTPException(
                status_code=413,
                detail="profile content exceeds 50k chars",
            )
        profile_mod.save_profile(new_content)
        return {"ok": True, "bytes": len(new_content.encode("utf-8"))}

    @app.get("/saved", response_class=HTMLResponse)
    async def get_saved(_user: str = Depends(require_auth)) -> HTMLResponse:
        items = sorted(
            _load_saved_items(),
            key=lambda i: i.get("saved_at", ""),
            reverse=True,
        )
        if not items:
            list_html = '<p class="empty">Nothing saved yet.</p>'
        else:
            rows = []
            for it in items:
                d = _html_escape(it.get("date", ""))
                iid = _html_escape(it.get("item_id", ""))
                ts = _html_escape((it.get("saved_at") or "")[:10])
                rows.append(
                    f'<li><a href="/briefing/{d}#{iid}">'
                    f'<span class="date">{d}</span>'
                    f'<span class="title">{iid}</span>'
                    f'<span class="muted">{ts}</span></a></li>'
                )
            list_html = '<ul class="briefings">' + "".join(rows) + "</ul>"
        body = _render_template(
            "saved.html",
            saved_list=list_html,
            saved_count=str(len(items)),
        )
        return HTMLResponse(body)

    @app.post("/briefing/{date}/item/{item_id}/save")
    async def post_save(
        date: str, item_id: str, _user: str = Depends(require_auth)
    ) -> dict:
        date, item_id = _validate_item_key(date, item_id)
        return toggle_saved(date, item_id)

    @app.post("/briefing/{date}/item/{item_id}/read")
    async def post_read(
        date: str, item_id: str, _user: str = Depends(require_auth)
    ) -> dict:
        date, item_id = _validate_item_key(date, item_id)
        return toggle_read(date, item_id)

    @app.post("/briefing/{date}/item/{item_id}/click")
    async def post_click(
        date: str, item_id: str, _user: str = Depends(require_auth)
    ) -> dict:
        date, item_id = _validate_item_key(date, item_id)
        return record_engagement(date, item_id, "click")

    # ---------------- Phase 9: follow-up Q&A ---------------------------------
    @app.get("/briefing/{date}/item/{item_id}/conversation")
    async def get_conversation(
        date: str, item_id: str, _user: str = Depends(require_auth)
    ) -> dict:
        """Return the persisted thread for a (date, item_id), or an empty
        record if no follow-up has been asked yet. The frontend renders
        whatever messages list comes back."""
        date, item_id = _validate_item_key(date, item_id)
        import follow_ups

        record = follow_ups.load_conversation(date, item_id)
        if record is None:
            return {
                "briefing_date": date,
                "item_id": item_id,
                "item_context": "",
                "messages": [],
            }
        return record

    @app.post("/briefing/{date}/item/{item_id}/ask")
    async def post_ask(
        date: str,
        item_id: str,
        request: Request,
        _user: str = Depends(require_auth),
    ) -> dict:
        """Run one follow-up turn for a briefing item.

        The agent runs in a background thread (``run_agent`` is sync) so
        the event loop stays responsive while a 30-second tool-using
        turn finishes. Returns the updated thread record.
        """
        date, item_id = _validate_item_key(date, item_id)

        # Refuse to start if the budget is already over the cap. Same
        # CLI-only override policy as /trigger/daily.
        try:
            budget.check_budget()
        except budget.BudgetExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=str(exc),
            )

        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="body must be JSON {question: ...}",
            )
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="question must not be empty",
            )
        if len(question) > 4000:
            raise HTTPException(
                status_code=413,
                detail="question exceeds 4000 chars",
            )

        path = config.BRIEFINGS_DIR / f"{date}.md"
        if not path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No briefing for {date}",
            )
        briefing_markdown = path.read_text(encoding="utf-8")

        import follow_ups

        try:
            record = await asyncio.to_thread(
                follow_ups.ask,
                date=date,
                item_id=item_id,
                question=question,
                briefing_markdown=briefing_markdown,
            )
        except ValueError as exc:
            # ValueErrors from follow_ups.ask are user input failures
            # (missing item, empty question, oversize). Surface as 400.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        except budget.BudgetExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — surface as 500 with type
            logger.exception("follow-up ask failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return record

    @app.get("/api/state/{date}")
    async def api_state(
        date: str, _user: str = Depends(require_auth)
    ) -> dict:
        """Return saved + read state for a single briefing date.

        Used by the briefing view's JS to render the right toggle states
        without scraping the page chrome.
        """
        if not _DATE_RE.match(date):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="date must be YYYY-MM-DD",
            )
        saved = {
            i["item_id"]
            for i in _load_saved_items()
            if i.get("date") == date
        }
        read_map = _load_read_state().get("read", {})
        read = {
            key.split(":", 1)[1]
            for key in read_map
            if key.startswith(f"{date}:")
        }
        return {"saved": sorted(saved), "read": sorted(read)}

    @app.get("/api/scheduler")
    async def api_scheduler(
        _user: str = Depends(require_auth),
    ) -> dict:
        """Observability for the in-process APScheduler.

        Uvicorn's default logging config swallows my module-level INFO
        logs, so there's no other way to confirm from outside the
        machine that the daily-briefing job is registered with the
        right next-fire timestamp. This route returns enough state to
        verify that without sshing into the container.
        """
        import scheduler as sched_mod

        inst = sched_mod.get_scheduler()
        if inst is None:
            return {"enabled": config.SCHEDULER_ENABLED, "running": False, "jobs": []}
        jobs = []
        for job in inst.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run_time": (
                        job.next_run_time.isoformat()
                        if job.next_run_time
                        else None
                    ),
                    "trigger": str(job.trigger),
                }
            )
        return {
            "enabled": config.SCHEDULER_ENABLED,
            "running": bool(inst.running),
            "jobs": jobs,
        }

    @app.get("/api/backup-snapshot")
    async def api_backup_snapshot(
        _user: str = Depends(require_auth),
    ) -> StreamingResponse:
        """Return a zip of the persistent data dir for the backup workflow.

        Phase 17: the GH Actions backup cron pulls this endpoint, extracts
        the archive, and commits the contents to the repo's ``data``
        branch. Including only the durable artifacts -- briefings,
        custom briefings, follow-ups, and memory. Skipping logs and the
        cost tracker because they are noisy and rebuildable.
        """
        import io
        import zipfile

        sources: list[tuple[Path, str]] = []
        for d in (
            config.BRIEFINGS_DIR,
            config.CUSTOM_BRIEFINGS_DIR,
            config.FOLLOW_UPS_DIR,
        ):
            if d.exists():
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        # arcname keeps the relative layout under data/.
                        rel = p.relative_to(config.DATA_DIR)
                        sources.append((p, str(rel).replace("\\", "/")))
        if config.MEMORY_FILE.exists():
            sources.append((config.MEMORY_FILE, "memory.json"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, arc in sources:
                zf.write(path, arc)
        buf.seek(0)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"backup-{stamp}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return app


# Module-level instance so `uvicorn dashboard:app` works.
app = create_app()
