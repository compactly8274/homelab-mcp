"""Tests for the Docker Registry v2 client."""

import pytest

from homelab_mcp.updater.registry import parse_image_ref


def test_parse_image_ref_full() -> None:
    r = parse_image_ref("lscr.io/linuxserver/radarr:latest")
    assert r.registry == "lscr.io"
    assert r.repository == "linuxserver/radarr"
    assert r.tag == "latest"
    assert r.digest is None


def test_parse_image_ref_no_registry() -> None:
    r = parse_image_ref("radarr:1.0")
    assert r.registry == "registry-1.docker.io"
    assert r.repository == "library/radarr"
    assert r.tag == "1.0"


def test_parse_image_ref_docker_hub_official() -> None:
    r = parse_image_ref("nginx:latest")
    assert r.registry == "registry-1.docker.io"
    assert r.repository == "library/nginx"


def test_parse_image_ref_docker_hub_user() -> None:
    r = parse_image_ref("alice/myapp:1.0")
    assert r.registry == "registry-1.docker.io"
    assert r.repository == "alice/myapp"
    assert r.tag == "1.0"


def test_parse_image_ref_with_digest() -> None:
    sha = "sha256:" + "a" * 64
    r = parse_image_ref(f"lscr.io/linuxserver/radarr:latest@{sha}")
    assert r.tag == "latest"
    assert r.digest == sha


def test_parse_image_ref_ghcr() -> None:
    r = parse_image_ref("ghcr.io/owner/img:latest")
    assert r.registry == "ghcr.io"
    assert r.repository == "owner/img"


def test_parse_image_ref_rejects_bad_digest() -> None:
    """An invalid digest format raises ValueError."""
    with pytest.raises(ValueError, match="invalid image digest"):
        parse_image_ref("ghcr.io/owner/img:latest@not-a-real-digest")


def test_parse_image_ref_default_tag() -> None:
    """A reference without a tag defaults to 'latest'."""
    r = parse_image_ref("lscr.io/linuxserver/radarr")
    assert r.tag == "latest"
