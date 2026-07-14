"""Docker Registry v2 client.

Implements the minimum needed to look up the current digest of an
``image:tag`` for drift detection. Speaks the v2 protocol:

1. ``GET /v2/<name>/manifests/<reference>`` returns the manifest.
2. The response carries a ``Docker-Content-Digest`` header (sha256 of
   the manifest bytes). We compare that to the local ``RepoDigests``.
3. If the registry returns 401, the ``Www-Authenticate: Bearer ...``
   header points to a token endpoint; we fetch a token and retry.

We do NOT use a Docker SDK on top of this. The endpoint is public,
returns a single header, and that's all we need.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx


log = logging.getLogger(__name__)


# Common registries. Bare "repo" (no slash) means Docker Hub.
# For Docker Hub, the canonical registry is registry-1.docker.io but
# the v2 path is the same.
DEFAULT_REGISTRY = "registry-1.docker.io"


@dataclass
class ImageRef:
    """A parsed docker image reference (without a registry alias)."""

    registry: str
    repository: str
    tag: str
    digest: str | None = None

    def __str__(self) -> str:
        base = f"{self.repository}:{self.tag}"
        if self.digest:
            base += f"@{self.digest}"
        if self.registry != DEFAULT_REGISTRY:
            base = f"{self.registry}/{base}"
        return base


@dataclass
class RegistryResult:
    """The result of a registry manifest fetch.

    ``kind`` is a small enum:
    - ``ok``              : the registry returned a digest
    - ``not_found``       : 404 — the image or tag doesn't exist
    - ``unauthorized``    : 401 we couldn't resolve (no token challenge)
    - ``transient_error`` : 5xx, network, timeout — worth retrying
    - ``protocol_error``  : 200 but malformed (e.g. missing Docker-Content-Digest)
    """

    kind: str
    digest: str | None = None
    status_code: int | None = None
    detail: str = ""


def parse_image_ref(image: str) -> ImageRef:
    """Parse a docker image reference.

    Examples:
        ``lscr.io/linuxserver/radarr:latest`` →
            registry=lscr.io, repo=linuxserver/radarr, tag=latest
        ``radarr:1.0`` → registry=registry-1.docker.io, repo=radarr, tag=1.0
        ``ghcr.io/owner/img:latest@sha256:abc`` →
            registry=ghcr.io, repo=owner/img, tag=latest, digest=sha256:abc
    """
    digest: str | None = None
    if "@" in image:
        image, digest = image.rsplit("@", 1)
        m = re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        if not m:
            raise ValueError(
                f"invalid image digest {digest!r}: must be 'sha256:<64 hex chars>'"
            )
    if ":" in image.rsplit("/", 1)[-1]:
        last = image.rsplit("/", 1)[-1]
        _, tag = last.rsplit(":", 1)
        repo_and_reg = image[: -len(last)] + last.rsplit(":", 1)[0]
    else:
        tag = "latest"
        repo_and_reg = image

    if "/" in repo_and_reg:
        first, rest = repo_and_reg.split("/", 1)
        if "." in first or ":" in first or first == "localhost":
            return ImageRef(registry=first, repository=rest, tag=tag, digest=digest)
        return ImageRef(registry=DEFAULT_REGISTRY, repository=repo_and_reg, tag=tag, digest=digest)
    if repo_and_reg == "library":
        return ImageRef(registry=DEFAULT_REGISTRY, repository="library", tag=tag, digest=digest)
    return ImageRef(registry=DEFAULT_REGISTRY, repository=f"library/{repo_and_reg}", tag=tag, digest=digest)


def _bearer_token(headers: httpx.Headers) -> tuple[str, dict[str, str]] | None:
    """Parse a Www-Authenticate: Bearer header.

    Returns (token_url, query_params) where query_params carries the
    service / scope / etc. fields. Returns None if the header doesn't
    look like a bearer challenge.
    """
    auth = headers.get("Www-Authenticate", "")
    m = re.search(r'realm="([^"]+)"', auth)
    if not m:
        return None
    realm = m.group(1)
    params: dict[str, str] = {}
    for k in ("service", "scope"):
        m2 = re.search(rf'{k}="([^"]+)"', auth)
        if m2:
            params[k] = m2.group(1)
    return realm, params


async def fetch_remote_digest(
    ref: ImageRef,
    *,
    timeout: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> RegistryResult:
    """Fetch the current digest of ``ref`` from its registry."""
    scheme = "https"
    url = f"{scheme}://{ref.registry}/v2/{ref.repository}/manifests/{ref.tag}"
    if ref.digest:
        url = f"{scheme}://{ref.registry}/v2/{ref.repository}/manifests/{ref.digest}"

    own_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout)

    try:
        r = await cli.get(
            url,
            headers={
                "Accept": (
                    "application/vnd.docker.distribution.manifest.v2+json,"
                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.oci.image.index.v1+json"
                )
            },
        )
        if r.status_code == 401:
            challenge = _bearer_token(r.headers)
            if challenge:
                token_url, token_params = challenge
                tok_r = await cli.get(token_url, params=token_params, timeout=timeout)
                if tok_r.status_code == 200:
                    token = tok_r.json().get("token") or tok_r.json().get("access_token")
                    if token:
                        r = await cli.get(
                            url,
                            headers={
                                "Accept": (
                                    "application/vnd.docker.distribution.manifest.v2+json,"
                                    "application/vnd.docker.distribution.manifest.list.v2+json,"
                                    "application/vnd.oci.image.manifest.v1+json,"
                                    "application/vnd.oci.image.index.v1+json"
                                ),
                                "Authorization": f"Bearer {token}",
                            },
                        )
        if r.status_code == 200:
            d = r.headers.get("Docker-Content-Digest")
            if d:
                return RegistryResult(kind="ok", digest=d, status_code=200)
            log.warning("registry %s returned 200 but no Docker-Content-Digest header", url)
            return RegistryResult(
                kind="protocol_error", status_code=200, detail="missing Docker-Content-Digest"
            )
        if r.status_code == 404:
            return RegistryResult(kind="not_found", status_code=404, detail=r.text[:200])
        if r.status_code == 401:
            return RegistryResult(kind="unauthorized", status_code=401, detail=r.text[:200])
        if 500 <= r.status_code < 600:
            return RegistryResult(
                kind="transient_error", status_code=r.status_code, detail=r.text[:200]
            )
        return RegistryResult(
            kind="protocol_error", status_code=r.status_code, detail=r.text[:200]
        )
    except (httpx.HTTPError, OSError) as e:
        log.warning("registry fetch failed for %s: %s", url, e)
        return RegistryResult(kind="transient_error", detail=f"{type(e).__name__}: {e}")
    finally:
        if own_client:
            await cli.aclose()
