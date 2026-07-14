"""Tests for the release-notes fetcher."""

from __future__ import annotations

import json
from typing import Any

import pytest

from homelab_mcp.updater.release_notes import (
    ReleaseNotes,
    fetch_release_notes,
    image_to_github_repo,
    is_probable_github_repo,
)


# -- image_to_github_repo --------------------------------------------------


def test_image_to_github_repo_ghcr() -> None:
    """ghcr.io/owner/image → github.com/owner/image."""
    assert image_to_github_repo("ghcr.io/owner/img") == "owner/img"


def test_image_to_github_repo_quay() -> None:
    """quay.io/owner/image → github.com/owner/image."""
    assert image_to_github_repo("quay.io/owner/img") == "owner/img"


def test_image_to_github_repo_lscr() -> None:
    """lscr.io/linuxserver/img → github.com/linuxserver/docker-img.

    LinuxServer's GH convention is ``docker-<name>``.
    """
    assert image_to_github_repo("lscr.io/linuxserver/radarr") == "linuxserver/docker-radarr"


def test_image_to_github_repo_ghcr_subpath() -> None:
    """ghcr.io/owner/sub/img → github.com/owner/sub."""
    assert image_to_github_repo("ghcr.io/owner/sub/img") == "owner/sub"


def test_image_to_github_repo_docker_hub_unknown() -> None:
    """A docker-hub user/image returns None (we'd have to look it up)."""
    assert image_to_github_repo("alice/myapp") is None


def test_image_to_github_repo_library_image() -> None:
    """A library/* image returns None."""
    assert image_to_github_repo("library/nginx") is None


# -- is_probable_github_repo ----------------------------------------------


def test_is_probable_github_repo_accepts() -> None:
    """A 'owner/repo' string is accepted."""
    assert is_probable_github_repo("owner/repo")
    assert is_probable_github_repo("linuxserver/docker-radarr")
    assert is_probable_github_repo("a/b-c_d")


def test_is_probable_github_repo_rejects() -> None:
    """No slashes, empty, or more than one slash rejected."""
    assert not is_probable_github_repo("")
    assert not is_probable_github_repo("nope")
    assert not is_probable_github_repo("a/b/c")
    assert not is_probable_github_repo("/b")
    assert not is_probable_github_repo("a/")


# -- fetch_release_notes --------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, text: str = "", payload: Any = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Mimics httpx.AsyncClient with a .get() that returns a script of canned responses."""

    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url: str, headers: dict | None = None, **kw) -> _FakeResp:
        self.calls.append((url, headers or {}))
        if not self._responses:
            raise AssertionError(f"unexpected GET: {url}")
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


async def test_fetch_release_notes_github_release() -> None:
    """GitHub release body becomes the notes text."""
    payload = {
        "tag_name": "v2.0.0",
        "name": "v2.0.0 — big release",
        "body": "## What's new\n- thing one\n- thing two\n",
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    out = await fetch_release_notes(
        "ghcr.io/owner/img", client=client  # type: ignore[arg-type]
    )
    assert out is not None
    assert "thing one" in out.text
    assert out.source == "github_release"
    assert out.tag == "v2.0.0"


async def test_fetch_release_notes_falls_back_to_changelog() -> None:
    """When the release API returns no body, the CHANGELOG.md is fetched."""
    release_payload = {"tag_name": "v2.0.0", "name": "v2.0.0", "body": ""}
    cl_text = "# Changelog\n\n## 2.0.0\n- first\n- second\n"
    client = _FakeClient([
        _FakeResp(200, payload=release_payload),  # release API
        _FakeResp(200, text=cl_text),             # CHANGELOG.md
    ])
    out = await fetch_release_notes("ghcr.io/owner/img", client=client)  # type: ignore[arg-type]
    assert out is not None
    assert out.source == "changelog"
    assert "first" in out.text


async def test_fetch_release_notes_returns_none_for_404() -> None:
    """404 on the release API returns None (we don't have notes to give)."""
    client = _FakeClient([
        _FakeResp(404, text="not found"),
        _FakeResp(404, text="not found"),
    ])
    out = await fetch_release_notes("ghcr.io/owner/img", client=client)  # type: ignore[arg-type]
    assert out is None


async def test_fetch_release_notes_returns_none_for_unknown_image() -> None:
    """An image we can't map to a github repo returns None."""
    client = _FakeClient([])
    out = await fetch_release_notes("alice/myapp", client=client)  # type: ignore[arg-type]
    assert out is None


async def test_fetch_release_notes_truncates_long_body() -> None:
    """Release bodies are capped at 8KB to keep the LLM prompt small."""
    long_body = "a" * 16_000
    payload = {"tag_name": "v1", "name": "v1", "body": long_body}
    client = _FakeClient([_FakeResp(200, payload=payload)])
    out = await fetch_release_notes("ghcr.io/owner/img", client=client)  # type: ignore[arg-type]
    assert out is not None
    assert len(out.text) <= 8192


async def test_fetch_release_notes_strips_html() -> None:
    """A release body that contains HTML has the tags stripped."""
    payload = {"tag_name": "v1", "name": "v1", "body": "<p>hello <b>world</b></p>"}
    client = _FakeClient([_FakeResp(200, payload=payload)])
    out = await fetch_release_notes("ghcr.io/owner/img", client=client)  # type: ignore[arg-type]
    assert out is not None
    assert "<p>" not in out.text
    assert "hello" in out.text and "world" in out.text
