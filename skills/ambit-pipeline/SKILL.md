---
name: ambit-pipeline
description: Master skill for Ambit — makes a Razorpay merchant safely transactable by AI buyers. Use when working on any part of Ambit, to understand execution order, which skill owns what, and the hard rules that apply across all of them.
---

# Ambit — master skill

Ambit is what a merchant installs to do business with AI agents safely. Three
capabilities, equal weight, one loop.

| Skill | Capability | Script (`execution/`) |
|---|---|---|
| [`bound-grants`](../bound-grants/SKILL.md) | Signed spending grants; ten deterministic checks | `issue_grant.py` |
| [`open-storefront`](../open-storefront/SKILL.md) | Machine-readable catalog + agent checkout | *(service — `src/ambit/open/`)* |
| [`explain-diagnostics`](../explain-diagnostics/SKILL.md) | Session timeline, failure classification, tamper-evident history | `verify_audit_chain.py` |
| [`self-anneal`](../self-anneal/SKILL.md) | Retry thresholds, fallback order, escalation, error classification | *(applied inside each)* |
| — | Test-mode capability probe | `probe_testmode.py` |
| — | Scenario suite / eval gate | `run_evals.py` |

## Execution order

```
 AI buyer agent (Claude Agent SDK)      ← the ONLY LLM near money,
      │  "restock the pantry, ₹3,000"      deliberately OUTSIDE the check
      ▼
 1. OPEN    agent reads catalog, builds a cart
      ▼
 2. BOUND   ALLOW / STEP_UP / DENY  ·  deterministic  ·  no LLM
      │
   allow├─────────────► Razorpay test API ──► Order + Payment Link
      │                         │
   deny│                   failure?
      ▼                         ▼
 3. EXPLAIN ◄──── every step, every reason, hash-chained
```

## Hard rules — apply to every skill below

1. **No LLM in the money-decision path.** A model can be argued into approving
   ₹1,00,000; a policy check cannot. The buyer agent is the only model in the
   system and it sits *outside* the gate. Where a model is used at all (failure
   labelling), it may only *describe* — never decide whether money moves.
   This is deliberate and gets a paragraph in `docs/ARCHITECTURE.md`, because
   Razorpay's judging criteria explicitly score *"where you chose not to use
   one."*
2. **Test mode only.** Every key must start with `rzp_test_`.
   `AMBIT_REQUIRE_TEST_MODE=true` makes the app refuse to start otherwise.
   Never print, log, echo, or commit a secret.
3. **Every money action is recorded before it is attempted**, not after. A
   crash mid-flight must leave evidence of intent.
4. **All ten BOUND checks run every time**, in fixed order — never
   short-circuit on first failure. A partial trace is a useless audit record.
5. **Deterministic first.** If a step can be plain code, it is plain code.
   Reach for a model only where judgment is genuinely required.
6. **Intermediates go to `.tmp/` only.** Never a deliverable.
7. **Secrets via environment variables**, read from `ambit/.env`. Referenced
   by name in docs, never by value.
8. **Log it in `docs/ENGINEERING-LOG.md` the day it happens.** The application
   form asks *"what broke, and how you got out"* and Razorpay says that's the
   answer they read first. Reconstructing it later produces fiction.

## Structure — and one deliberate deviation

The workspace playbook (`UNIVERSAL-CLAUDE.md`, Playbook 2) specifies
`skills/<name>/SKILL.md` + `execution/*.py` at ~1:1. Ambit follows that for
every standalone operation, but it is a **running service**, not a scheduled
linear pipeline — a FastAPI app cannot be expressed as one-script-per-skill.

So:

```
skills/<name>/SKILL.md   ← WHAT and the RULES          (conforms)
execution/*.py           ← standalone deterministic ops (conforms, ~1:1)
src/ambit/               ← the service itself           (deviation, justified)
.tmp/                    ← intermediates only           (conforms)
```

**Tool-first still applies:** before writing new code, check `execution/` for
an existing script that's an ~80%+ match and extend it rather than duplicating.

## Definition of done

A step is done when the script ran clean **and** the real output reflects it —
the API responded, the chain verifies, the console shows it. "Exited 0" is not
verification.
