"""Dashboard route tests for v1-routines.

Coverage targets the seams that R6 introduced:
  - Data branch reads via raw URL fetch (with caching)
  - Data branch listing via Contents API
  - Request-write helpers (queue_follow_up, queue_custom_briefing)
  - Status-poll routes (/follow-up/{id}/status, /custom/status/{id})
  - Local-state routes (saved/read/profile) survived the rewrite

httpx is mocked via respx so no real GitHub traffic occurs.
"""

from __future__ import annotations

import base64
import json

import pytest
import respx
from httpx import Response


# ---------------------------------------------------------------------------
# helpers to build raw / api URLs the dashboard uses
# ---------------------------------------------------------------------------
SLUG = "emstacho-su/ai-news-agent-routines"
BRANCH = "data"


def raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{SLUG}/{BRANCH}/{path}"


def api_url(path: str) -> str:
    return f"https://api.github.com/repos/{SLUG}/contents/{path}"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def test_health_no_auth(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_branch"] == f"{SLUG}#{BRANCH}"


def test_index_requires_auth(client):
    r = client.get("/")
    assert r.status_code == 401


def test_index_wrong_password(client):
    r = client.get("/", auth=("stack", "wrong"))
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# briefings list + detail (data branch reads)
# ---------------------------------------------------------------------------
@respx.mock
def test_index_lists_briefings_from_data_branch(client, auth):
    respx.get(api_url("briefings")).mock(
        return_value=Response(
            200,
            json=[
                {
                    "name": "2026-05-08.md",
                    "path": "briefings/2026-05-08.md",
                    "sha": "deadbeef",
                    "size": 1234,
                    "type": "file",
                },
                {
                    "name": "2026-05-09.md",
                    "path": "briefings/2026-05-09.md",
                    "sha": "feedface",
                    "size": 1500,
                    "type": "file",
                },
                {
                    "name": "README.md",  # should be filtered by regex
                    "path": "briefings/README.md",
                    "sha": "cafebabe",
                    "size": 100,
                    "type": "file",
                },
            ],
        )
    )

    r = client.get("/", auth=auth)
    assert r.status_code == 200
    body = r.text
    # Newest first
    assert body.find("2026-05-09") < body.find("2026-05-08")
    # README is filtered out
    assert "README" not in body or "/briefing/README" not in body


@respx.mock
def test_briefing_detail_renders_markdown(client, auth):
    respx.get(raw_url("briefings/2026-05-08.md")).mock(
        return_value=Response(
            200,
            text="# Briefing\n\n## TL;DR\n- one\n- two\n",
        )
    )
    r = client.get("/briefing/2026-05-08", auth=auth)
    assert r.status_code == 200
    assert "<h1>Briefing</h1>" in r.text
    assert "<li>one</li>" in r.text


@respx.mock
def test_briefing_detail_404_when_missing(client, auth):
    respx.get(raw_url("briefings/2099-01-01.md")).mock(
        return_value=Response(404)
    )
    r = client.get("/briefing/2099-01-01", auth=auth)
    assert r.status_code == 404


def test_briefing_detail_rejects_bad_date(client, auth):
    r = client.get("/briefing/notadate", auth=auth)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# request queueing
# ---------------------------------------------------------------------------
@respx.mock
def test_ask_queues_follow_up_request(client, auth):
    captured = {}

    def capture(request):
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return Response(201, json={"content": {"sha": "newsha"}})

    respx.put(api_url("requests")).mock(side_effect=capture)
    # The dashboard PUTs to the per-file URL, not the dir URL. Match anything
    # under requests/ via a regex.
    respx.put(url__regex=rf"https://api.github.com/repos/{SLUG}/contents/requests/.+\.json").mock(
        side_effect=capture
    )

    r = client.post(
        "/briefing/2026-05-08/item/some-topic-id/ask",
        json={"question": "What does this mean for X?", "item_headline": "Some headline"},
        auth=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "follow_up"
    assert "request_id" in data

    # Verify the request file payload that the dashboard PUT
    sent = captured["body"]
    decoded = json.loads(base64.b64decode(sent["content"]).decode("utf-8"))
    assert decoded["kind"] == "follow_up"
    assert decoded["briefing_date"] == "2026-05-08"
    assert decoded["topic_id"] == "some-topic-id"
    assert decoded["question"] == "What does this mean for X?"
    assert decoded["item_headline"] == "Some headline"


def test_ask_rejects_empty_question(client, auth):
    r = client.post(
        "/briefing/2026-05-08/item/x/ask",
        json={"question": "  "},
        auth=auth,
    )
    assert r.status_code == 400


@respx.mock
def test_trigger_custom_queues_request(client, auth):
    captured = {}

    def capture(request):
        captured["body"] = json.loads(request.content)
        return Response(201, json={"content": {"sha": "newsha"}})

    respx.put(
        url__regex=rf"https://api.github.com/repos/{SLUG}/contents/custom_requests/.+\.json"
    ).mock(side_effect=capture)

    r = client.post(
        "/trigger/custom",
        json={"focus": "Quantum cryptography this month"},
        auth=auth,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "custom_briefing"
    assert data["slug"] == "quantum-cryptography-this-month"

    decoded = json.loads(base64.b64decode(captured["body"]["content"]).decode("utf-8"))
    assert decoded["focus"] == "Quantum cryptography this month"
    assert decoded["slug"] == "quantum-cryptography-this-month"


# ---------------------------------------------------------------------------
# status polling
# ---------------------------------------------------------------------------
@respx.mock
def test_follow_up_status_queued_when_no_response(client, auth):
    rid = "1234567890123-abcdef12"
    respx.get(raw_url(f"follow_ups/{rid}.md")).mock(return_value=Response(404))
    r = client.get(f"/follow-up/{rid}/status", auth=auth)
    assert r.status_code == 200
    assert r.json() == {"status": "queued", "request_id": rid}


@respx.mock
def test_follow_up_status_ready_when_response_present(client, auth):
    rid = "1234567890123-abcdef12"
    respx.get(raw_url(f"follow_ups/{rid}.md")).mock(
        return_value=Response(
            200, text="---\nrequest_id: " + rid + "\n---\n\n# Follow-up\n\nThe answer."
        )
    )
    r = client.get(f"/follow-up/{rid}/status", auth=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["request_id"] == rid
    assert "The answer" in body["answer_md"]


def test_follow_up_status_rejects_bad_id(client, auth):
    r = client.get("/follow-up/not-a-real-id/status", auth=auth)
    assert r.status_code == 400


@respx.mock
def test_custom_status_queued_when_no_match(client, auth):
    rid = "1234567890123-abcdef12"
    respx.get(api_url("custom_briefings")).mock(return_value=Response(200, json=[]))
    r = client.get(f"/custom/status/{rid}", auth=auth)
    assert r.status_code == 200
    assert r.json() == {"status": "queued", "request_id": rid}


@respx.mock
def test_custom_status_ready_when_match_in_frontmatter(client, auth):
    rid = "1234567890123-abcdef12"
    respx.get(api_url("custom_briefings")).mock(
        return_value=Response(
            200,
            json=[
                {
                    "name": "2026-05-08_quantum.md",
                    "path": "custom_briefings/2026-05-08_quantum.md",
                    "sha": "x",
                    "size": 999,
                    "type": "file",
                }
            ],
        )
    )
    respx.get(raw_url("custom_briefings/2026-05-08_quantum.md")).mock(
        return_value=Response(
            200,
            text=f"---\nrequest_id: {rid}\nfocus: \"Quantum\"\n---\n\n# Custom\n",
        )
    )
    r = client.get(f"/custom/status/{rid}", auth=auth)
    body = r.json()
    assert body["status"] == "ready"
    assert body["filename"] == "2026-05-08_quantum.md"
    assert body["url"] == "/custom/2026-05-08_quantum.md"


# ---------------------------------------------------------------------------
# Profile + saved + read endpoints were removed for the Vercel deploy
# (filesystem is ephemeral). The helper functions remain in dashboard.py
# for a future iteration that backs them with the data branch.
# ---------------------------------------------------------------------------
def test_profile_endpoint_removed(client, auth):
    r = client.get("/profile", auth=auth)
    assert r.status_code == 404


def test_saved_endpoint_removed(client, auth):
    r = client.get("/saved", auth=auth)
    assert r.status_code == 404


def test_save_toggle_endpoint_removed(client, auth):
    r = client.post("/briefing/2026-05-08/item/topic-a/save", auth=auth)
    assert r.status_code == 404
