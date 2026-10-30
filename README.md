# jev-stack — local decision gate for AI task intake

jev-stack provides bounded-schema decision endpoints — enum/bool/int/number fields, batched
contexts, per-field probabilities — used as a cheap reflex layer in front of expensive model
calls. Intake, routing, and triage calls that would otherwise burn a full chat completion get
answered in milliseconds by a small local model, with escalation to stronger models only where
confidence is low. Tier-1 talks to any llama.cpp `parallel-decision` fork endpoint; tier-2 and
tier-3 serve the identical protocol through an adapter backed by any OpenAI-compatible chat
endpoint.

## Components

- `jev_client.py` — `decide(schema, contexts, instructions, tier2=, tier3=)`. Byte-stable
  serialization keeps requests prefix-cache friendly; batches up to 256 contexts per call;
  applies a `min_confidence` filter to verdicts; fails open — on any transport, schema, or
  endpoint error the affected contexts get `None` verdicts instead of an exception. Every
  verdict carries a `_tier` tag naming the tier that produced it. With `tier2` set, contexts
  landing in the gray-zone band `(0.4, 0.95)` re-decide at tier-2; with `tier3` set, tier-2
  reject-flags get one confirmation pass before they stick.
- `jev_adapter.py` — decision-protocol adapter. Exposes `POST /v1/decision` and services each
  request with exactly ONE chat call to `OMNI_MODEL` (default `glm-5.3-flash`; env-overridable
  to any OpenAI-compatible endpoint). This is how tier-2/3 serve the same schema contract as
  the local tier-1 endpoint.
- `systemd/` — unit files for a flash-tier adapter (tier-2, port 8496) and a main-model tier-3
  adapter (port 8495).

## Benchmarks (10-context task-admission triage, RTX 4060)

| tier | model                    | ms/decision                  | accuracy            |
|------|--------------------------|------------------------------|---------------------|
| 1    | local qwen2.5-0.5B q8    | 22                           | 5/10                |
| 2    | glm-5.3-flash (adapter)  | ~2700 cold; 0.4 prefix-cached | 9/10               |
| 3    | glm-5.3 (main)           | confirm-only                 | 10/10 combined stack|

Notes:

- Local small models false-reject real tasks at high confidence. Never hard-block on tier-1
  alone.
- Keep tier-1 for enum-pick reflexes; reserve judgment for tiers 2–3.

## Contract rules

- Every schema field REQUIRES a `description`; the decision endpoint returns 400 otherwise.
- Field types are `enum`, `boolean`, `integer`, `number` only. No free text.
