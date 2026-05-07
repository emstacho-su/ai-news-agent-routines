"""Stack profile loader.

Per SPEC section 11, data/profile.md holds 'about Stack' context that
the agent receives in its system prompt every run. The dashboard lets
Stack edit it live; this module owns reading + writing the file with
the same atomic-write pattern as memory.py and budget.py.

The file is intentionally markdown so it can be both human-edited and
fed to the model verbatim. The default content seeds it on first read
so a fresh checkout doesn't ship with a stale `about` block.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)


# Default profile content -- shipped on first load if the file doesn't
# exist yet. Stack edits this through the dashboard at /profile, so the
# default is a starting point, not the canonical version.
DEFAULT_PROFILE = """# About Stack

Junior at Syracuse iSchool, studying Information Management & Technology
with a security concentration.

## Daily-driver tools
- Claude Code (primary dev environment)
- NinjaTrader (algorithmic trading)
- Python, FastAPI, Anthropic SDK
- Windows 11, PowerShell + bash

## Interest priorities
1. Developer tools and frameworks (Claude Code, MCP, agent libraries,
   dev environments)
2. Model releases (especially Anthropic, also OpenAI / Google when
   capability-changing)
3. Research breakthroughs with reproducible results
4. Security-adjacent AI work (prompt injection, sandboxing, model
   evaluations)

## Tone preferences
- Concise. Skip preamble and conclusions.
- Technical. Don't hand-hold; assume working knowledge of agents,
  RAG, tool use, prompt caching, etc.
- No marketing fluff or hype. Real capability changes only.
- Cite sources inline.

## What to skip
- Beginner-level explanations of LLMs / transformers / agents
- Funding / valuation news unless tied to a real product change
- Drama, palace intrigue, X-thread chains
"""


def load_profile(path: Optional[Path] = None) -> str:
    """Return the current profile contents, seeding the default if absent.

    First read on a fresh checkout writes DEFAULT_PROFILE to disk so the
    agent has stable context immediately. Subsequent edits via the
    dashboard overwrite the file.
    """
    target = path if path is not None else config.PROFILE_FILE
    if not target.exists():
        logger.info("seeding default profile at %s", target)
        save_profile(DEFAULT_PROFILE, target)
        return DEFAULT_PROFILE
    return target.read_text(encoding="utf-8")


def save_profile(content: str, path: Optional[Path] = None) -> None:
    """Atomically replace profile.md with `content`."""
    target = path if path is not None else config.PROFILE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def system_prompt_with_profile(base_prompt: str) -> str:
    """Prepend the current profile to `base_prompt`.

    Used by main.cmd_run_daily and dashboard._run_daily_job so both
    entry points feed the agent the same context. The profile sits
    BEFORE the base prompt so SPEC section 17's instructions still
    have the last word on agent behavior.
    """
    profile = load_profile().rstrip()
    return f"{profile}\n\n---\n\n{base_prompt}"
