---
name: explain-diagnostics
description: Explain what an AI buyer did and why it failed, and prove the record has not been edited. Use when working on Ambit's session timeline, failure classification, recovery stopping rules, the hash chain, or the reported metrics.
---

# EXPLAIN — prove what happened

**Goal.** Not "did the card decline." What the **agent** did, why it was
refused, what was attempted next, and a record a sceptic can verify
themselves.

**Scripts:** `execution/verify_audit_chain.py`, `execution/run_evals.py`
**Service:** `src/ambit/explain/`

## The four pieces

| File | Job |
|---|---|
| `chain.py` | append-only hash chain, `sha256(prev_hash ‖ canonical_json(entry))` |
| `classify.py` | 15 failure classes, deterministic, precedence-ordered |
| `recovery.py` | what happens next, with a stopping rule |
| `report.py` | session timeline and the metrics |

## Hard rules

1. **No model anywhere in this layer.** Classification reads the binding
   check, Razorpay error codes and the catalog. `PROMPT_INJECTION` is
   pattern-matched against the product text the agent was exposed to, never
   inferred by asking a model whether it feels manipulated. A classifier that
   can be talked out of its own diagnosis is not a diagnosis.
2. **A `POLICY` denial is never retried.** The sharpest rule in the build. A
   refused purchase is the system working, and retrying it is a security bug
   wearing resilience as a costume. "The agent tried 40 times and the 40th got
   through" is the exact failure this project exists to prevent.
3. **At most two automated attempts,** then escalate to a human. Escalation is
   terminal. `MAX_AUTOMATED_ATTEMPTS = 2` lives in `recovery.py`, not scattered
   through callers.
4. **Injection outranks the plain limit reading.** Hitting a cap and being
   talked into hitting one are different events for a merchant, so the class
   must say which happened.
5. **One canonical JSON.** `canonical.py` is used for both signing and
   hashing. Two "canonical" encoders is a bug waiting for a deadline.
6. **Never rewrite history to make a number look better.** The chain is
   append-only. A correction is a new entry.

## Precedence order in `classify_session`

Order matters more than any individual rule here. It is, deliberately:

1. a payment that actually reached the rail and failed there
2. a refusal where the cart contained hostile copy → `PROMPT_INJECTION`
3. repetition without progress (3 or more identical refusals) → `AGENT_LOOPED`
4. straight from the binding check → `LIMIT_HIT`, `OUT_OF_SCOPE`, and so on
5. `STEP_UP` → `NEEDS_HUMAN`
6. a healthy session → `NONE`
7. anything left → `UNCLASSIFIED`, flagged rather than guessed

Every class carries **blame** (agent, principal, merchant, rail) and a
**recoverability**: `POLICY`, `TRANSIENT`, `PERMANENT` or `AMBIGUOUS`.

## Where a model would be allowed, and is not wired in

Labelling the genuinely ambiguous residue, the `UNCLASSIFIED` cases the rules
could not name. That is a labelling job on an already-final decision, after
the money question is settled, and it can never change an outcome. The hook
exists; nothing is wired to it, because nothing has needed it. If you wire it
up, it must not be able to change `recoverability`.

## Honest metrics

- `money_moved` counts **captured** money only. An authorised but unpaid order
  is a hold, not revenue.
- `money_stopped` is the total of what was asked for and refused. It ships
  with a note saying it is **not a claim about fraud prevented**. Do not remove
  that note, and do not put the number in a headline without it.
- `unresolved` lists what still needs a human. An empty list must mean empty,
  not filtered.

## Verification

```
python execution/verify_audit_chain.py             # exits 1 on a broken chain
python execution/verify_audit_chain.py --self-test # proves it catches tampering
python execution/run_evals.py                      # 63 labelled scenarios
```

`--self-test` builds a throwaway chain, verifies it, edits a past row, and
fails unless the break is caught. A verifier that always says VERIFIED is
worse than no verifier, because people believe it.

The eval suite asserts the outcome **and** the binding check. The right answer
for the wrong reason is still a bug: an audit trail whose reason codes cannot
be trusted is worse than none.
