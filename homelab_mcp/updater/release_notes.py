"""Release-notes fetcher.

Resolves an image reference to a likely GitHub repo, then fetches the
release notes for the most recent release. Strategy:

1. If the image is on ghcr.io / quay.io, the GitHub repo is
   ``<owner>/<image>`` (with a couple of well-known conventions like
   LinuxServer's ``docker-<name>``).
2. Try ``GET https://api.github.com/repos/<repo>/releases/latest``.
3. If the release body is empty/missing, try ``CHANGELOG.md`` from
   the default branch (``raw.githubusercontent.com``).
4. Truncate the result to 8 KB; release notes are for an LLM
   classification, not a full read-through.

Never raises. Returns None when no notes can be fetched.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

MAX_NOTES_BYTES = 8 * 1024


@dataclass
class ReleaseNotes:
    """A release-notes text payload for one image's current release.

    Attributes:
        text:    the human-readable notes (already stripped of HTML,
                 truncated to MAX_NOTES_BYTES)
        tag:     the tag/version the notes describe (e.g. "v2.0.0")
        source:  "github_release" or "changelog" or "github_release_html"
    """

    text: str
    tag: str = ""
    source: str = ""


# -- image → GitHub repo --------------------------------------------------


def image_to_github_repo(image: str) -> str | None:
    """Best-effort mapping from a docker image to a GitHub ``owner/repo``.

    Returns None when we don't recognize the registry layout.
    """
    if not image or "/" not in image:
        return None
    # Strip a leading "library/" (Docker Hub official images).
    if image.startswith("library/"):
        return None
    parts = image.split("/", 1)
    registry, rest = parts[0], parts[1]
    if registry == "ghcr.io":
        # ghcr.io/owner/image -> owner/image
        # ghcr.io/owner/sub/image -> owner/sub (skip the first segment
        # past owner; most ghcr.io repos have 2 segments: owner/repo)
        sub = rest.split("/")
        if len(sub) == 1:
            # owner only — not enough info
            return None
        if len(sub) == 2:
            return f"{sub[0]}/{sub[1]}"
        # 3+ segments: owner / sub-repo / image; use first two
        return f"{sub[0]}/{sub[1]}"
    if registry == "quay.io":
        sub = rest.split("/")
        if len(sub) >= 2:
            return f"{sub[0]}/{sub[1]}"
        return None
    if registry == "lscr.io":
        # lscr.io/linuxserver/<name> -> linuxserver/docker-<name>
        if rest.startswith("linuxserver/"):
            name = rest.split("/", 1)[1]
            return f"linuxserver/docker-{name}"
        return None
    # Docker Hub: any 'user/repo' string we'd need to look up via
    # the Docker Hub API. Skip for now (we can wire this later).
    return None


def is_probable_github_repo(s: str) -> bool:
    """A 'owner/repo' string. Exactly one slash, both parts non-empty."""
    if not s or not isinstance(s, str):
        return False
    parts = s.split("/")
    if len(parts) != 2:
        return False
    return bool(parts[0]) and bool(parts[1])


# -- text cleanup ----------------------------------------------------------


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    """Strip HTML tags and decode entities. Best-effort; not a real parser."""
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _truncate(s: str, *, n: int = MAX_NOTES_BYTES) -> str:
    if len(s) <= n:
        return s
    return s[: n - 80] + "\n\n[...truncated; full notes at the upstream URL...]"


# -- fetcher ---------------------------------------------------------------


class _HttpGet(Protocol):
    async def get(self, url: str, headers: dict | None = None, **kw) -> Any: ...
    async def aclose(self) -> None: ...


async def _get_release(repo: str, client: _HttpGet) -> dict[str, Any] | None:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    r = await client.get(url, headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception as e:
        log.debug("github release json parse failed: %s", e)
        return None


async def _get_changelog(repo: str, client: _HttpGet) -> str | None:
    """Fetch the top of the CHANGELOG.md (or CHANGELOG.rst) from the default branch."""
    for filename in ("CHANGELOG.md", "CHANGELOG.rst", "changelog.md"):
        url = f"https://raw.githubusercontent.com/{repo}/HEAD/{filename}"
        r = await client.get(url, headers={"Accept": "text/plain"})
        if r.status_code == 200 and r.text and r.text.strip():
            return r.text
    return None


async def fetch_release_notes(
    image: str,
    *,
    client: _HttpGet | None = None,
) -> ReleaseNotes | None:
    """Fetch release notes for an image's current release. Never raises.

    Returns None if the image can't be mapped to a GitHub repo or if
    every fetch attempt fails.
    """
    repo = image_to_github_repo(image)
    if not is_probable_github_repo(repo or ""):
        return None

    own_client = client is None
    if own_client:
        import httpx
        client = httpx.AsyncClient(timeout=10.0)  # type: ignore[assignment]

    try:
        rel = await _get_release(repo, client)  # type: ignore[arg-type]
        if rel is not None:
            body = rel.get("body") or ""
            if body.strip():
                text = _strip_html(body) if "<" in body else body.strip()
                return ReleaseNotes(
                    text=_truncate(text), tag=rel.get("tag_name", ""),
                    source="github_release",
                )
        # Fall through to CHANGELOG.md
        cl = await _get_changelog(repo, client)  # type: ignore[arg-type]
        if cl:
            return ReleaseNotes(text=_truncate(cl), tag="", source="changelog")
        return None
    except (OSError, Exception) as e:
        log.warning("release-notes fetch failed for %s: %s", image, e)
        return None
    finally:
        if own_client and client is not None:
            await client.aclose()
