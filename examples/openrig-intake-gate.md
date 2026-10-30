# Gating OpenRig queue intake with jev-stack
Reference implementation: an ops-board whose `/api/delegate` handler runs every inbound task
through jev-stack before it reaches the rig queue. Tier-1 (local, ~22 ms) decides first;
contexts landing in the gray-zone band escalate to tier-2 (flash adapter). Verdicts attach to
the job record as advisory metadata. Every call fails open — on timeout, HTTP error, or
malformed verdict the handler proceeds with `null` fields — so the board never hard-depends on
the AI endpoints being up.

## Flow

    caller
      -> gated intake  (tier-1 local decision, 22ms)
           | gray zone (any field 0.40-0.95 or missing)?
           -> tier-2 flash adapter (~2.7s cold, one batched call)
                | reject flagged?
                -> tier-3 main-model confirm (bounded: rejects only)
      -> rig queue create
      -> seat claims qitem

## Code sketch (node, intake handler)

```js
const JEV_URL = 'http://127.0.0.1:8497/v1/decision';   // tier-1 (local)
const TIER2_URL = 'http://127.0.0.1:8496/v1/decision'; // adapter -> flash
const SCHEMA = {
  needs_planning: { type: 'boolean', description: 'true if the task needs a written plan before execution' },
  model_class:    { type: 'enum', enum: ['flash', 'main', 'local'], description: 'compute class for the task' },
  reject:         { type: 'boolean', description: 'true if the text is not a real actionable task' },
};
const score = async (url, text, timeoutMs) => {
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions: 'You triage task admissions for an agent ops board.',
                             schema: SCHEMA, contexts: [String(text).slice(0, 512)] }),
      signal: AbortSignal.timeout(timeoutMs) });
    if (!r.ok) return null;
    const item = ((await r.json()).results || [])[0];
    if (!item) return null;
    const prob = (k) => Number((item.fields[k] || {}).probability) || 0;
    return { decision: item.decision || {}, prob: { needs_planning: prob('needs_planning'),
               model_class: prob('model_class'), reject: prob('reject') } };
  } catch { return null; }   // fail-open
};
// in the delegate handler, before enqueue:
const specText = String(spec || task || '').slice(0, 512);
const t1 = await score(JEV_URL, specText, 3000);
const gray = (v) => !v || Object.values(v.prob).some((p, i) =>
  Object.values(v.decision)[i] == null || (p >= 0.40 && p < 0.95));
let verdict = t1;
if (gray(t1)) verdict = (await score(TIER2_URL, specText, 120000)) || t1;
// attach advisory metadata, keep enqueue unconditional:
job.jev = verdict && { model_class: verdict.decision.model_class,
                       needs_planning: !!verdict.decision.needs_planning,
                       rejectProb: verdict.prob.reject };
```

## Why outside-core

OpenRig's community-harness contract keeps built-ins unchanged and the daemon owns qitems with
no admission hook. This pattern needs zero daemon changes: it composes with any intake wrapper
that ultimately calls `rig queue create`, and it disappears cleanly (fail-open) when the
decision endpoints are down.

## Honest limits

- Tier-1 small models are reflexes, not judges: 5/10 on admission-style triage in our bench,
  with high-confidence false rejects on real tasks. Strong on lexical enum picks. Never
  hard-block on tier-1 alone.
- Tier-2 latency lands on the intake hot path (~2.7 s cold per gray-zone batch; near-zero on
  prefix-cache hits).
- Spam verdicts stay advisory (logged, attached) until calibrated on live traffic.

Benchmarks and the full three-tier client live in the repo README.
