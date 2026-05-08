"""FastAPI dashboard for the AI News Agent (Routines edition).

Phase R6 rewrite. The v1 dashboard ran the agent loop in-process and
served briefings off a Fly volume. v1-routines pushes all generation
into Claude Code Routines; this dashboard's job is reduced to:

  - HTTP basic auth gate
  - Read briefings + custom briefings from the GitHub `data` branch
  - Render markdown to HTML
  - Profile editor (still local-file backed; mounted at /app/data on Fly)
  - Saved / read state (local JSON files; user state, not agent state)
  - Queue follow-up + custom-briefing requests by writing JSON files to
    the data branch via the GitHub Contents API
  - Poll for response files to surface back to the user

What was stripped vs v1: the in-process agent loop, APScheduler, SSE
activity stream, budget tracker, Resend notifications, all of /trigger
that previously called Anthropic. See `docs/r1-deviations.md` and
`docs/routines-version-plan.md` §6 for the redesign rationale.

Auth model: single password via env var DASHBOARD_PASSWORD; constant-
time compare. Username is ignored.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import markdown as markdown_lib
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

import config
import profile as profile_mod

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
_security = HTTPBasic()


def _expected_password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def require_auth(
    credentials: HTTPBasicCredentials = Depends(_security),
) -> str:
    expected = _expected_password()
    if not expected:
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
# GitHub data-branch client
# ---------------------------------------------------------------------------
# Read path: raw.githubusercontent.com (public, no auth, CDN-cached).
# Listing path: api.github.com /contents/<dir>?ref=data (no auth needed
# for public repos but rate-limited harder).
# Write path: api.github.com /contents/<path> with Authorization Bearer
# token; required for queue request files. Token is GITHUB_PAT env.
_RAW_BASE = "https://raw.githubusercontent.com/{slug}/{branch}/{path}"
_API_BASE = "https://api.github.com/repos/{slug}/contents/{path}"

# Tiny in-memory TTL cache so a single page render doesn't hammer the
# GitHub API. List endpoints get a short TTL; file content gets a longer
# one. Cache cleared on dashboard restart (which is fine).
_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl_seconds: float) -> Any:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_seconds: float) -> None:
    _CACHE[key] = (time.time() + ttl_seconds, value)


def _gh_headers(*, write: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    pat = config.GITHUB_PAT
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    elif write:
        # Write requires auth; surface a clean error rather than a 401
        # from GitHub later.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITHUB_PAT not configured; cannot queue requests.",
        )
    return headers


def _data_branch_raw_url(path: str) -> str:
    return _RAW_BASE.format(
        slug=config.GITHUB_REPO_SLUG,
        branch=config.GITHUB_DATA_BRANCH,
        path=path,
    )


async def _fetch_data_file(path: str, *, ttl_seconds: float = 60.0) -> Optional[str]:
    """Fetch a single file from the data branch, returning its text body
    or None on 404."""
    cache_key = f"file:{path}"
    cached = _cache_get(cache_key, ttl_seconds)
    if cached is not None:
        return cached if cached != "__NOT_FOUND__" else None

    url = _data_branch_raw_url(path)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
    if resp.status_code == 404:
        _cache_set(cache_key, "__NOT_FOUND__", ttl_seconds)
        return None
    resp.raise_for_status()
    text = resp.text
    _cache_set(cache_key, text, ttl_seconds)
    return text


async def _list_data_dir(dir_path: str, *, ttl_seconds: float = 30.0) -> list[dict]:
    """List files in a data-branch directory via the GitHub Contents API.
    Returns [{name, path, sha, size}, ...] for entries of type 'file'."""
    cache_key = f"dir:{dir_path}"
    cached = _cache_get(cache_key, ttl_seconds)
    if cached is not None:
        return cached

    url = _API_BASE.format(slug=config.GITHUB_REPO_SLUG, path=dir_path)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            url,
            params={"ref": config.GITHUB_DATA_BRANCH},
            headers=_gh_headers(),
        )
    if resp.status_code == 404:
        _cache_set(cache_key, [], ttl_seconds)
        return []
    resp.raise_for_status()
    payload = resp.json()
    files = [
        {"name": e["name"], "path": e["path"], "sha": e["sha"], "size": e.get("size", 0)}
        for e in payload
        if e.get("type") == "file"
    ]
    _cache_set(cache_key, files, ttl_seconds)
    return files


async def _put_data_file(path: str, body: str, *, message: str) -> dict:
    """PUT a file onto the data branch via the Contents API. Always
    creates new files only; for updates pass the existing sha which we
    don't need here (request files are unique by id)."""
    headers = _gh_headers(write=True)
    url = _API_BASE.format(slug=config.GITHUB_REPO_SLUG, path=path)
    payload = {
        "message": message,
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "branch": config.GITHUB_DATA_BRANCH,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.put(url, json=payload, headers=headers)
    if resp.status_code not in (200, 201):
        logger.error(
            "Contents API PUT failed: status=%d body=%s", resp.status_code, resp.text
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"GitHub Contents API rejected the write ({resp.status_code}).",
        )
    # Bust list cache for the parent dir so the new file shows up.
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    _CACHE.pop(f"dir:{parent}", None)
    return resp.json()


# ---------------------------------------------------------------------------
# Briefing listing + parsing
# ---------------------------------------------------------------------------
_BRIEFING_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_v(\d+))?\.md$")
_CUSTOM_FILENAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_([a-z0-9][a-z0-9-]+?)(?:_v(\d+))?\.md$"
)


async def _list_briefings() -> list[dict]:
    files = await _list_data_dir("briefings")
    out: list[dict] = []
    for f in files:
        m = _BRIEFING_FILENAME_RE.match(f["name"])
        if not m:
            continue
        out.append(
            {
                "date": m.group(1),
                "version": int(m.group(2)) if m.group(2) else 1,
                "filename": f["name"],
                "size_bytes": f["size"],
            }
        )
    out.sort(key=lambda b: (b["date"], b["version"]), reverse=True)
    return out


async def _list_custom_briefings() -> list[dict]:
    files = await _list_data_dir("custom_briefings")
    out: list[dict] = []
    for f in files:
        m = _CUSTOM_FILENAME_RE.match(f["name"])
        if not m:
            continue
        out.append(
            {
                "date": m.group(1),
                "slug": m.group(2),
                "version": int(m.group(3)) if m.group(3) else 1,
                "filename": f["name"],
                "size_bytes": f["size"],
            }
        )
    out.sort(key=lambda b: (b["date"], b["slug"], b["version"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Local user state — saved items + read flags + profile
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _validate_item_key(date: str, item_id: str) -> tuple[str, str]:
    if not _DATE_RE.match(date):
        raise HTTPException(400, f"date must be YYYY-MM-DD, got {date!r}")
    if not _ITEM_ID_RE.match(item_id):
        raise HTTPException(400, f"item_id must be lowercase kebab-case, got {item_id!r}")
    return date, item_id


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("state file %s is unreadable; returning default", path)
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_saved() -> list[dict]:
    return _load_json(config.SAVED_ITEMS_FILE, [])


def _load_read() -> dict:
    return _load_json(config.DASHBOARD_STATE_FILE, {"read": {}})


def toggle_saved(date: str, item_id: str) -> dict:
    items = _load_saved()
    key = f"{date}:{item_id}"
    existing = next(
        (i for i in items if f"{i.get('date')}:{i.get('item_id')}" == key),
        None,
    )
    if existing:
        items = [i for i in items if i is not existing]
        is_saved = False
    else:
        items.append({"date": date, "item_id": item_id, "saved_at": _now_iso()})
        is_saved = True
    _save_json(config.SAVED_ITEMS_FILE, items)
    return {"saved": is_saved, "date": date, "item_id": item_id}


def toggle_read(date: str, item_id: str) -> dict:
    state = _load_read()
    rmap = state.setdefault("read", {})
    key = f"{date}:{item_id}"
    if rmap.get(key):
        del rmap[key]
        is_read = False
    else:
        rmap[key] = _now_iso()
        is_read = True
    _save_json(config.DASHBOARD_STATE_FILE, state)
    return {"read": is_read, "date": date, "item_id": item_id}


# ---------------------------------------------------------------------------
# Request queueing
# ---------------------------------------------------------------------------
def _new_request_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


_REQUEST_ID_RE = re.compile(r"^\d+-[a-f0-9]{8}$")
_SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, *, max_len: int = 40) -> str:
    s = _SLUG_SAFE_RE.sub("-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "topic"


async def queue_follow_up(
    *,
    briefing_date: str,
    topic_id: Optional[str],
    item_headline: Optional[str],
    question: str,
) -> dict:
    if not question or not question.strip():
        raise HTTPException(400, "question is required")
    if not (topic_id or item_headline):
        raise HTTPException(400, "topic_id or item_headline is required")
    rid = _new_request_id()
    payload = {
        "id": rid,
        "kind": "follow_up",
        "created_at": _now_iso(),
        "briefing_date": briefing_date,
        "topic_id": topic_id or "",
        "item_headline": item_headline or "",
        "question": question.strip(),
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    await _put_data_file(
        f"requests/{rid}.json",
        body,
        message=f"chore(request): follow-up {rid}",
    )
    return {"request_id": rid, "kind": "follow_up"}


async def queue_custom_briefing(*, focus: str) -> dict:
    if not focus or not focus.strip():
        raise HTTPException(400, "focus is required")
    focus = focus.strip()
    rid = _new_request_id()
    payload = {
        "id": rid,
        "kind": "custom_briefing",
        "created_at": _now_iso(),
        "focus": focus,
        "slug": _slugify(focus),
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    await _put_data_file(
        f"custom_requests/{rid}.json",
        body,
        message=f"chore(request): custom briefing {rid}",
    )
    return {"request_id": rid, "kind": "custom_briefing", "slug": payload["slug"]}


async def check_follow_up_response(request_id: str) -> Optional[str]:
    """Return the response markdown if the processor has written it,
    else None. Short cache so repeat polls don't hammer GitHub."""
    return await _fetch_data_file(
        f"follow_ups/{request_id}.md", ttl_seconds=15.0
    )


async def find_custom_response(request_id: str) -> Optional[dict]:
    """Search custom_briefings/ for a file with frontmatter matching the
    request_id. Returns {filename, body} or None."""
    files = await _list_data_dir("custom_briefings", ttl_seconds=15.0)
    for f in files:
        body = await _fetch_data_file(f["path"], ttl_seconds=60.0)
        if not body:
            continue
        # Frontmatter check: look for the request_id line in the first 400 chars
        head = body[:400]
        if f"request_id: {request_id}" in head:
            return {"filename": f["name"], "body": body}
    return None


# ---------------------------------------------------------------------------
# HTML templating
# ---------------------------------------------------------------------------
def _h(s: str) -> str:
    return html.escape(s, quote=True)


def _render_template(name: str, **subs: str) -> str:
    template = (config.PROJECT_ROOT / "static" / name).read_text(encoding="utf-8")
    out = template
    for k, v in subs.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def _render_briefings_list(briefings: list[dict]) -> str:
    if not briefings:
        return '<p class="empty">No briefings yet. The daily routine fires at 12:00 UTC.</p>'
    items = []
    for b in briefings:
        version_html = (
            f'<span class="version-tag">v{b["version"]}</span>'
            if b["version"] > 1
            else ""
        )
        href = f"/briefing/{b['date']}"
        if b["version"] > 1:
            href += f"/v{b['version']}"
        items.append(
            f'<li><a href="{href}">'
            f'<span class="date">{b["date"]}</span>'
            f'<span class="title">Briefing for {b["date"]}{version_html}</span>'
            f"</a></li>"
        )
    return '<ul class="briefings">' + "".join(items) + "</ul>"


def _render_custom_list(custom: list[dict]) -> str:
    if not custom:
        return '<p class="empty">No custom briefings yet. Click RUN CUSTOM BRIEFING to queue one.</p>'
    items = []
    for c in custom:
        version_html = (
            f'<span class="version-tag">v{c["version"]}</span>'
            if c["version"] > 1
            else ""
        )
        href = f"/custom/{c['filename']}"
        items.append(
            f'<li><a href="{href}">'
            f'<span class="date">{c["date"]}</span>'
            f'<span class="title">{_h(c["slug"].replace("-", " "))}{version_html}</span>'
            f"</a></li>"
        )
    return '<ul class="briefings">' + "".join(items) + "</ul>"


def _render_saved_list(saved: list[dict]) -> str:
    if not saved:
        return '<p class="empty">No saved items yet.</p>'
    items = [
        f'<li><a href="/briefing/{i["date"]}#{_h(i["item_id"])}">'
        f'<span class="date">{i["date"]}</span>'
        f'<span class="title">{_h(i["item_id"])}</span>'
        f"</a></li>"
        for i in saved
    ]
    return '<ul class="briefings">' + "".join(items) + "</ul>"


def _markdown_to_html(md_text: str) -> str:
    return markdown_lib.markdown(
        md_text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )


# ---------------------------------------------------------------------------
# Lifespan + app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # No background tasks in v1-routines. Routines own all generation;
    # the dashboard is purely an HTTP read/write surface.
    logger.info(
        "dashboard starting; data branch %s/%s, %s",
        config.GITHUB_REPO_SLUG,
        config.GITHUB_DATA_BRANCH,
        "(write enabled)" if config.GITHUB_PAT else "(read-only — GITHUB_PAT not set)",
    )
    yield
    logger.info("dashboard shutting down")


def create_app() -> FastAPI:
    app = FastAPI(lifespan=_lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=str(config.PROJECT_ROOT / "static")),
        name="static",
    )

    # -----------------------------------------------------------------
    # Briefings
    # -----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(_: str = Depends(require_auth)) -> HTMLResponse:
        briefings = await _list_briefings()
        html_body = _render_template(
            "index.html",
            briefings_list=_render_briefings_list(briefings),
            briefing_count=str(len(briefings)),
        )
        return HTMLResponse(html_body)

    @app.get("/briefing/{date}", response_class=HTMLResponse)
    async def briefing(date: str, _: str = Depends(require_auth)) -> HTMLResponse:
        if not _DATE_RE.match(date):
            raise HTTPException(400, "date must be YYYY-MM-DD")
        md_text = await _fetch_data_file(f"briefings/{date}.md", ttl_seconds=300.0)
        if md_text is None:
            raise HTTPException(404, f"No briefing for {date}")
        html_body = _render_template(
            "briefing.html",
            briefing_date=date,
            briefing_html=_markdown_to_html(md_text),
        )
        return HTMLResponse(html_body)

    @app.get("/briefing/{date}/v{version}", response_class=HTMLResponse)
    async def briefing_versioned(
        date: str, version: int, _: str = Depends(require_auth)
    ) -> HTMLResponse:
        if not _DATE_RE.match(date):
            raise HTTPException(400, "date must be YYYY-MM-DD")
        md_text = await _fetch_data_file(
            f"briefings/{date}_v{version}.md", ttl_seconds=300.0
        )
        if md_text is None:
            raise HTTPException(404, f"No v{version} briefing for {date}")
        html_body = _render_template(
            "briefing.html",
            briefing_date=date,
            briefing_html=_markdown_to_html(md_text),
        )
        return HTMLResponse(html_body)

    # -----------------------------------------------------------------
    # Custom briefings
    # -----------------------------------------------------------------
    @app.get("/custom", response_class=HTMLResponse)
    async def custom(_: str = Depends(require_auth)) -> HTMLResponse:
        custom_briefings = await _list_custom_briefings()
        html_body = _render_template(
            "custom.html",
            custom_list=_render_custom_list(custom_briefings),
            custom_count=str(len(custom_briefings)),
        )
        return HTMLResponse(html_body)

    @app.get("/custom/status/{request_id}")
    async def custom_status(
        request_id: str, _: str = Depends(require_auth)
    ) -> JSONResponse:
        if not _REQUEST_ID_RE.match(request_id):
            raise HTTPException(400, "invalid request_id")
        match = await find_custom_response(request_id)
        if match is None:
            return JSONResponse({"status": "queued", "request_id": request_id})
        return JSONResponse(
            {
                "status": "ready",
                "request_id": request_id,
                "filename": match["filename"],
                "url": f"/custom/{match['filename']}",
            }
        )

    @app.get("/custom/{filename}", response_class=HTMLResponse)
    async def custom_view(
        filename: str, _: str = Depends(require_auth)
    ) -> HTMLResponse:
        if not _CUSTOM_FILENAME_RE.match(filename):
            raise HTTPException(400, "invalid custom briefing filename")
        md_text = await _fetch_data_file(
            f"custom_briefings/{filename}", ttl_seconds=300.0
        )
        if md_text is None:
            raise HTTPException(404, f"No custom briefing {filename}")
        html_body = _render_template(
            "briefing.html",
            briefing_date=filename.removesuffix(".md"),
            briefing_html=_markdown_to_html(md_text),
        )
        return HTMLResponse(html_body)

    @app.post("/trigger/custom")
    async def trigger_custom(
        body: dict, _: str = Depends(require_auth)
    ) -> JSONResponse:
        focus = (body or {}).get("focus", "")
        result = await queue_custom_briefing(focus=focus)
        return JSONResponse(result)

    # -----------------------------------------------------------------
    # Follow-ups
    # -----------------------------------------------------------------
    @app.post("/briefing/{date}/item/{item_id}/ask")
    async def ask(
        date: str,
        item_id: str,
        body: dict,
        _: str = Depends(require_auth),
    ) -> JSONResponse:
        date, item_id = _validate_item_key(date, item_id)
        question = (body or {}).get("question", "")
        item_headline = (body or {}).get("item_headline", "")
        result = await queue_follow_up(
            briefing_date=date,
            topic_id=item_id,
            item_headline=item_headline or item_id,
            question=question,
        )
        return JSONResponse(result)

    @app.get("/follow-up/{request_id}/status")
    async def follow_up_status(
        request_id: str, _: str = Depends(require_auth)
    ) -> JSONResponse:
        if not _REQUEST_ID_RE.match(request_id):
            raise HTTPException(400, "invalid request_id")
        md = await check_follow_up_response(request_id)
        if md is None:
            return JSONResponse({"status": "queued", "request_id": request_id})
        return JSONResponse(
            {
                "status": "ready",
                "request_id": request_id,
                "answer_md": md,
            }
        )

    # -----------------------------------------------------------------
    # Profile / saved / read endpoints removed for the Vercel deploy.
    # Vercel's filesystem is ephemeral per invocation, so local JSON
    # state files don't persist. Helpers are kept in this module (see
    # toggle_saved / toggle_read / _load_saved above) so a future
    # iteration can move them to the data branch and re-expose the
    # routes. For now the dashboard is read + queue only.
    # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # Health (unauthenticated; small smoke surface for Fly checks)
    # -----------------------------------------------------------------
    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": config.VERSION,
                "data_branch": f"{config.GITHUB_REPO_SLUG}#{config.GITHUB_DATA_BRANCH}",
                "write_enabled": bool(config.GITHUB_PAT),
            }
        )

    return app


app = create_app()
