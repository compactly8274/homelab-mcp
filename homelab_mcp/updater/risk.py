"""LLM-based risk classifier for an image's release notes.

Speaks the OpenAI Chat Completions protocol, which is also what
Ollama, vLLM, llama.cpp (with a thin shim), and MiniMax M3 expose.

The classifier returns a :class:`RiskVerdict` with one of three
buckets: ``SAFE``, ``CAUTION``, or ``BREAKING``. The auto-apply
pipeline uses this to decide whether to apply a pending update
without manual approval.

Fallback policy: any LLM error (5xx, network, JSON-parse) is treated
as a CAUTION verdict. The reasoning: a classifier outage should not
deaden the auto-apply pipeline. Better to over-apply than to
under-classify and silently roll forward.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


log = logging.getLogger(__name__)


VALID_RISKS = ("SAFE", "CAUTION", "BREAKING")


@dataclass
class RiskVerdict:
    """The classification result of an LLM risk check.

    Attributes:
        risk:             one of "SAFE" / "CAUTION" / "BREAKING"
        summary:          one-line description
        migration_steps:  list of human actions required after the update
        compose_changes:  list of compose.yaml edits required
        env_changes:      list of env-var additions/removals
    """

    risk: str
    summary: str
    migration_steps: list[str] = field(default_factory=list)
    compose_changes: list[str] = field(default_factory=list)
    env_changes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalize the risk string to one of the three valid buckets.
        if isinstance(self.risk, str):
            self.risk = self.risk.strip().upper()
            if self.risk not in VALID_RISKS:
                self.risk = "CAUTION"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskVerdict":
        """Build a verdict, coercing unknown risk strings to CAUTION."""
        return cls(
            risk=_coerce_risk(d.get("risk")),
            summary=str(d.get("summary") or "").strip(),
            migration_steps=_coerce_str_list(d.get("migration_steps")),
            compose_changes=_coerce_str_list(d.get("compose_changes")),
            env_changes=_coerce_str_list(d.get("env_changes")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "summary": self.summary,
            "migration_steps": self.migration_steps,
            "compose_changes": self.compose_changes,
            "env_changes": self.env_changes,
        }


# -- JSON extraction --------------------------------------------------------


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_LOOSE = re.compile(r"\{[^{}]*\"risk\"[^{}]*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a possibly-noisy LLM response."""
    if not text:
        return None
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = _JSON_LOOSE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        return None


def _coerce_risk(value: Any) -> str:
    if not isinstance(value, str):
        return "CAUTION"
    s = value.strip().upper()
    if s in VALID_RISKS:
        return s
    return "CAUTION"


def _coerce_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [line.strip("-* ").strip() for line in value.splitlines() if line.strip()]
    return []


# -- classifier -------------------------------------------------------------


class _HttpPost(Protocol):
    async def post(self, url: str, json: dict | None = None,
                   headers: dict | None = None, **kw) -> Any: ...
    async def aclose(self) -> None: ...


_SYSTEM_PROMPT = (
    "You are a conservative release-notes classifier for a self-hosted homelab. "
    "You will be given release notes for a docker image that is about to be "
    "auto-updated. Classify the update into one of three risk buckets:\n"
    "  - SAFE      : bug fixes, security patches, perf, dependency bumps; "
    "                no user action required\n"
    "  - CAUTION   : user action REQUIRED after the update (db migration, "
    "                restart order, data folder move, new mandatory env, "
    "                backwards-compatible new feature, deprecation)\n"
    "  - BREAKING  : backwards-incompatible; new image can't replace the old "
    "                one without a config change (e.g. total env-var rename, "
    "                dropped API, db format change, port change)\n\n"
    "Return ONLY a single JSON object with these exact keys:\n"
    '  "risk":            "SAFE" | "CAUTION" | "BREAKING"\n'
    '  "summary":         one-sentence plain English\n'
    '  "migration_steps": list of human actions after the update (empty if SAFE)\n'
    '  "compose_changes": list of compose.yaml edits required (empty if none)\n'
    '  "env_changes":     list of env-var additions/removals (empty if none)\n'
    "Do not include any commentary. JSON only."
)


async def classify_release_notes(
    *,
    endpoint: str,
    model: str,
    notes_text: str,
    api_key: str = "",
    timeout: float = 30.0,
    client: _HttpPost | None = None,
) -> RiskVerdict:
    """Classify ``notes_text`` via the OpenAI-compatible chat completions API.

    Returns a CAUTION verdict on any LLM error so the auto-apply pipeline
    is not blocked by a transient classifier outage.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)  # type: ignore[assignment]

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": notes_text[:MAX_NOTES_PROMPT]},
        ],
        "temperature": 0.0,
        "max_tokens": 600,
        # Ollama ignores response_format; OpenAI requires it.
        "response_format": {"type": "json_object"},
    }

    try:
        try:
            r = await client.post(endpoint, json=body, headers=headers, timeout=timeout)  # type: ignore[arg-type]
        except TypeError:
            # Some test fakes don't accept timeout kwarg
            r = await client.post(endpoint, json=body, headers=headers)  # type: ignore[arg-type]
        if r.status_code >= 500:
            log.warning("classifier 5xx: status=%d", r.status_code)
            return _caution_fallback("5xx from classifier")
        if r.status_code >= 400:
            log.warning("classifier 4xx: status=%d body=%s", r.status_code, getattr(r, "text", "")[:200])
            return _caution_fallback("4xx from classifier")
        try:
            data = r.json()
        except Exception as e:
            log.warning("classifier json parse failed: %s", e)
            return _caution_fallback("classifier returned non-JSON")
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            log.warning("classifier shape unexpected: %s", e)
            return _caution_fallback("classifier returned unexpected shape")
        parsed = _extract_json(content)
        if not parsed:
            log.warning("classifier did not return JSON: %r", content[:200])
            return _caution_fallback("classifier did not return JSON")
        return RiskVerdict.from_dict(parsed)
    except (httpx.HTTPError, OSError, Exception) as e:
        log.warning("classifier network error: %s", e)
        return _caution_fallback(f"{type(e).__name__}: {e}")
    finally:
        if own_client and client is not None:
            await client.aclose()


def _caution_fallback(reason: str) -> RiskVerdict:
    return RiskVerdict(
        risk="CAUTION",
        summary=f"classifier fallback to CAUTION ({reason})",
        migration_steps=[],
        compose_changes=[],
        env_changes=[],
    )


MAX_NOTES_PROMPT = 6000


class RiskError(Exception):
    """Raised by callers that want to distinguish a CAUTION-fallback
    from a real CAUTION verdict. The auto-apply pipeline treats both
    the same way; this exists for diagnostics.
    """
