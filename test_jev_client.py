"""Live integration test: jev_client against http://127.0.0.1:8497/v1/decision."""

import json
import sys
import urllib.error
import urllib.request

from jev_client import ENDPOINT, decide

SCHEMA = {
    "lane": {"enum": ["orchestrator", "runner", "sentry"], "description": "who handles this"},
    "needs_human": {"type": "boolean", "description": "true if irreversible or spend"},
}
CONTEXTS = [
    "Deploy usstech.net to Netlify",
    "Fix README typo",
    "Rotate prod DB creds with irreversible DROP",
]
INSTRUCTIONS = "Assign a lane and state whether a human must approve."
LANES = {"orchestrator", "runner", "sentry"}

def _endpoint_up() -> bool:
    body = json.dumps(
        {"instructions": INSTRUCTIONS, "schema": SCHEMA, "contexts": ["ping"]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False

def main() -> int:
    if not _endpoint_up():
        print(f"SKIP: JEV decision endpoint not reachable at {ENDPOINT}")
        return 0

    results = decide(SCHEMA, CONTEXTS, INSTRUCTIONS, min_confidence=0.0, timeout=6.0)

    assert isinstance(results, list), "results must be a list"
    assert len(results) == 3, f"expected 3 results, got {len(results)}"
    for context, result in zip(CONTEXTS, results):
        assert result is not None, f"fail-open None for: {context}"
        decision = result["decision"]
        fields = result["fields"]
        lane = decision["lane"]
        assert lane in LANES, f"lane {lane!r} not in enum for: {context}"
        prob = fields["lane"]["probability"]
        assert isinstance(prob, float), f"lane probability not a float: {prob!r}"
        assert "probability" in fields["needs_human"], "needs_human probability missing"
        needs_human = decision["needs_human"]
        assert isinstance(needs_human, bool), f"needs_human not boolean: {needs_human!r}"
        print(f"OK  lane={lane:<12} needs_human={needs_human!s:<5} p={prob:.2f}  {context}")
    print("PASS: 3/3 contexts validated")
    return 0

if __name__ == "__main__":
    sys.exit(main())
