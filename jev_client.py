"""JEV decision client: byte-stable batched requests against the local verdict endpoint."""

import json
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

ENDPOINT = "http://127.0.0.1:8497/v1/decision"
TIER2_ENDPOINT = "http://127.0.0.1:8496/v1/decision"
TIER3_ENDPOINT = "http://127.0.0.1:8495/v1/decision"
TIER2_BAND = (0.4, 0.95)
BATCH_SIZE = 256
# Frozen contract string. INSTRUCTIONS = "You pre-triage gates for agent factory."

@dataclass
class Decision:
    name: str
    value: Any = None
    probability: float = 0.0

    def as_pair(self) -> Tuple[Any, float]:
        return (self.value, self.probability)

    @classmethod
    def from_fields(cls, fields: Dict[str, Dict[str, Any]]) -> List["Decision"]:
        return [
            cls(name, meta.get("value"), float(meta.get("probability", 0.0)))
            for name, meta in (fields or {}).items()
        ]

def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]

def _post(body: str, timeout: float) -> dict:
    req = urllib.request.Request(
        ENDPOINT,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=max(timeout, 120.0)) as resp:  # tier-2 backend is an LLM roundtrip, not 22ms local
        return json.loads(resp.read().decode("utf-8"))

def _escalate(
    schema: dict,
    contexts: List[str],
    instructions: str,
    results: List[Optional[dict]],
    min_confidence: float,
    timeout: float,
    tier2_url: str,
    band: Tuple[float, float],
) -> List[Optional[dict]]:
    indexes: List[int] = []
    for i, result in enumerate(results):
        if result is None:
            indexes.append(i)
            continue
        for meta in (result.get("fields") or {}).values():
            prob = meta.get("probability")
            if (
                isinstance(prob, (int, float))
                and not isinstance(prob, bool)
                and band[0] <= prob < band[1]
            ):
                indexes.append(i)
                break
    if not indexes or timeout <= 0.0:
        return results

    body = json.dumps(
        {
            "instructions": instructions,
            "schema": schema,
            "contexts": [contexts[i] for i in indexes],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        req = urllib.request.Request(
            tier2_url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=max(timeout, 120.0)) as resp:  # tier-2 = LLM roundtrip
            entries = json.loads(resp.read().decode("utf-8"))["results"]
        if len(entries) != len(indexes):
            raise ValueError("tier2 result count mismatch")
        for pos, i in enumerate(indexes):
            item = entries[pos] or {}
            decision = dict(item.get("decision") or {})
            fields = item.get("fields") or {}
            for name, meta in fields.items():
                prob = meta.get("probability")
                if (
                    isinstance(prob, (int, float))
                    and not isinstance(prob, bool)
                    and prob < min_confidence
                ):
                    decision[name] = None
            results[i] = {"decision": decision, "fields": fields, "_tier": 2}
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"jev_client: tier2 escalation failed ({exc}); keeping tier-1", file=sys.stderr)
    return results

def _tier3_confirm(
    schema: dict,
    contexts: List[str],
    instructions: str,
    results: List[Optional[dict]],
    tier3_url: str,
    timeout: float = 180.0,
) -> List[Optional[dict]]:
    indexes: List[int] = []
    for i, result in enumerate(results):
        if result is not None and (result.get("decision") or {}).get("reject") is True:
            indexes.append(i)
    if not indexes:
        return results

    body = json.dumps(
        {
            "instructions": instructions,
            "schema": schema,
            "contexts": [contexts[i] for i in indexes],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        req = urllib.request.Request(
            tier3_url,
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=max(timeout, 180.0)) as resp:
            entries = json.loads(resp.read().decode("utf-8"))["results"]
        if len(entries) != len(indexes):
            raise ValueError("tier3 result count mismatch")
        for pos, i in enumerate(indexes):
            item = entries[pos] or {}
            results[i] = {
                "decision": dict(item.get("decision") or {}),
                "fields": item.get("fields") or {},
                "_tier": 3,
            }
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"jev_client: tier3 confirmation failed ({exc}); keeping tier-2", file=sys.stderr)
    return results

def decide(
    schema: dict,
    contexts: List[str],
    instructions: str,
    min_confidence: float = 0.75,
    timeout: float = 2.0,
    tier2: bool = False,
    tier2_url: str = TIER2_ENDPOINT,
    tier2_band: Tuple[float, float] = TIER2_BAND,
    tier3: bool = False,
    tier3_url: str = TIER3_ENDPOINT,
) -> List[Optional[dict]]:
    if not contexts:
        return []

    # Byte-stable tail (instructions + schema, sorted keys) built ONCE for all contexts.
    tail = json.dumps(
        {"instructions": instructions, "schema": schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    deadline = time.monotonic() + timeout

    try:
        collected: List[Optional[dict]] = []
        for chunk in _chunks(contexts, BATCH_SIZE):
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError("decision deadline exceeded")
            body = '{"contexts":' + json.dumps(chunk, separators=(",", ":")) + "," + tail[1:]
            payload = _post(body, remaining)
            for item in payload["results"]:
                decision = dict(item.get("decision") or {})
                fields = item.get("fields") or {}
                for name, meta in fields.items():
                    prob = meta.get("probability")
                    if (
                        isinstance(prob, (int, float))
                        and not isinstance(prob, bool)
                        and prob < min_confidence
                    ):
                        decision[name] = None
                collected.append({"decision": decision, "fields": fields})
        if len(collected) != len(contexts):
            raise ValueError("result count mismatch")
        if tier2:
            collected = _escalate(
                schema,
                contexts,
                instructions,
                collected,
                min_confidence,
                max(deadline - time.monotonic(), 0.0),
                tier2_url,
                tier2_band,
            )
        if tier3:
            collected = _tier3_confirm(schema, contexts, instructions, collected, tier3_url)
        return collected
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f"jev_client: decision endpoint failed ({exc}); fail-open", file=sys.stderr)
        return [None] * len(contexts)

def gate_pretriage(text: str, min_confidence: float = 0.8) -> dict:
    schema = {
        "needs_deep_review": {"type": "boolean"},
        "risk_tier": {"enum": ["low", "med", "high"]},
    }
    result = decide(schema, [text], INSTRUCTIONS, min_confidence=min_confidence)[0]
    if result is None:
        return {"needs_deep_review": True, "risk_tier": "high"}
    decision = result["decision"]
    if decision.get("needs_deep_review") is None:
        decision["needs_deep_review"] = True
    if decision.get("risk_tier") is None:
        decision["risk_tier"] = "high"
    return decision
