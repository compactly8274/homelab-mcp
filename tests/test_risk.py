"""Tests for the LLM risk classifier."""

from __future__ import annotations

from typing import Any

from homelab_mcp.updater.risk import (
    RiskVerdict,
    classify_release_notes,
)


class _FakeResp:
    def __init__(self, status_code: int, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload

    async def aread(self) -> bytes:
        import json as _json
        return _json.dumps(self._payload or {}).encode()


class _FakeClient:
    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None, headers: dict | None = None, **kw) -> _FakeResp:
        self.calls.append((url, headers or {}))
        if not self._responses:
            raise AssertionError("unexpected POST")
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


# -- happy path ------------------------------------------------------------


async def test_classify_safe_response() -> None:
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": (
                '{"risk": "SAFE", "summary": "tiny patch release",'
                ' "migration_steps": [], "compose_changes": []}'
            )}
        }]
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    v = await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="patch notes",
        client=client,  # type: ignore[arg-type]
    )
    assert v.risk == "SAFE"
    assert v.summary == "tiny patch release"


async def test_classify_caution_response() -> None:
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": (
                '{"risk": "CAUTION", "summary": "db schema change",'
                ' "migration_steps": ["run migrator"], "compose_changes": []}'
            )}
        }]
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    v = await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="db migrator",
        client=client,  # type: ignore[arg-type]
    )
    assert v.risk == "CAUTION"
    assert "run migrator" in v.migration_steps


async def test_classify_breaking_response() -> None:
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": (
                '{"risk": "BREAKING", "summary": "drops v1 api",'
                ' "migration_steps": ["rebuild clients"],'
                ' "compose_changes": ["add new env var"]}'
            )}
        }]
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    v = await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="v2",
        client=client,  # type: ignore[arg-type]
    )
    assert v.risk == "BREAKING"


# -- error path: classifier outage → CAUTION -------------------------------


async def test_classify_unparseable_response_falls_back_to_caution() -> None:
    """A response with non-JSON content is a CAUTION fallback."""
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": "I am not JSON, sorry"}
        }]
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    v = await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="x",
        client=client,  # type: ignore[arg-type]
    )
    assert v.risk == "CAUTION"


async def test_classify_5xx_falls_back_to_caution() -> None:
    """A 5xx is a CAUTION fallback (better to over-apply than under-classify)."""
    client = _FakeClient([_FakeResp(503, text="upstream down")])
    v = await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="x",
        client=client,  # type: ignore[arg-type]
    )
    assert v.risk == "CAUTION"


async def test_classify_auth_sends_bearer_header() -> None:
    """When api_key is set, the request includes the Bearer header."""
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": (
                '{"risk": "SAFE", "summary": "ok",'
                ' "migration_steps": [], "compose_changes": []}'
            )}
        }]
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="x",
        api_key="sk-test",
        client=client,  # type: ignore[arg-type]
    )
    _url, headers = client.calls[0]
    assert headers.get("Authorization", "").startswith("Bearer sk-test")


async def test_classify_omits_bearer_when_no_api_key() -> None:
    """Without an api_key, no Authorization header is set (works for local Ollama)."""
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": (
                '{"risk": "SAFE", "summary": "ok",'
                ' "migration_steps": [], "compose_changes": []}'
            )}
        }]
    }
    client = _FakeClient([_FakeResp(200, payload=payload)])
    await classify_release_notes(
        endpoint="http://x/v1/chat/completions",
        model="m",
        notes_text="x",
        client=client,  # type: ignore[arg-type]
    )
    _url, headers = client.calls[0]
    assert "Authorization" not in headers


# -- RiskVerdict dataclass -------------------------------------------------


def test_risk_verdict_risk_is_normalized() -> None:
    """Lowercase risk strings are normalized to uppercase enum values."""
    v = RiskVerdict(risk="safe", summary="x")
    assert v.risk == "SAFE"


def test_risk_verdict_unknown_risk_defaults_to_caution() -> None:
    """An unknown risk string is coerced to CAUTION (safer than SAFE)."""
    v = RiskVerdict(risk="MAYBE", summary="x")
    assert v.risk == "CAUTION"
