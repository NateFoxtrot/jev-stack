Title: Pattern proposal: local decision gate before queue intake (reference impl + benches)

Body:

We've been running a small pattern outside OpenRig that other rig operators might benefit
from, and I'd like to know where (if anywhere) it fits the contracts you've shipped in 0.7.3.

**The pattern.** A gated intake wrapper in front of `rig queue create`. Every inbound task
text is scored by a local bounded-schema decision endpoint (enum/boolean/integer/number
fields, batched contexts, per-field probabilities — a llama.cpp "parallel-decision" build,
~22ms on a 0.5B) before enqueue. Gray-zone verdicts (any field probability in 0.40-0.95, or
missing) escalate to one batched call against an ordinary chat model through a decision-
protocol adapter. Verdicts attach to the job as advisory metadata; every layer fails open,
so intake never depends on the gate being up.

**Why we run it.** Queue hygiene (obvious non-tasks get flagged before a seat ever claims
them) and quota economics (the 22ms local tier settles the easy calls; the API tier only
sees the gray zone — measured ~6 of 10 contexts in our bench).

**Benches** (10-context admission triage, RTX 4060):
- local 0.5B tier-1: 22ms/decision, 5/10 accuracy — a reflex, not a judge; high-confidence
  false rejects on real tasks, so we keep blocks parked
- + flash tier-2: 9/10, gray-zone only
- + main-model tier-3 confirm on rejects: 10/10, main quota spend bounded to flagged rejects

Reference implementation (python stdlib + node sketch, MIT): natefoxtrot/jev-stack, with an
`examples/openrig-intake-gate.md` written against the community-harness contract's
outside-core stance.

**The question.** Zero daemon changes are needed for this, so it can live as a standalone
repo forever. But if you see value for rig operators generally: would you want it as (a) a
docs example under docs/, (b) a line in the integration-author contract, or (c) nothing in
core until a promotion-test case emerges? Happy to shape it whichever way fits.
