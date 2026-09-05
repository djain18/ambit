---
name: bound-grants
description: Decide whether an AI agent may spend money, using signed grants and ten deterministic checks. Use when implementing or debugging Ambit's spending limits, grant issuing/revocation, or the ALLOW/STEP_UP/DENY decision path.
---

# BOUND — signed spending grants

**Goal.** Give an AI agent bounded, revocable, auditable spending authority,
and decide every money request against it with zero ambiguity.

**Script:** `execution/issue_grant.py` (issue / inspect / revoke)
**Service:** `src/ambit/bound/`

Real-world analogue: Razorpay's [UPI Reserve Pay](https://razorpay.com/blog/upi-reserve-pay/)
(live — ₹10,000 cap, 90-day validity, revocable, real-time debit
notification), and AP2-style mandates. Checked 2026-08-30.

## The grant

Ed25519-signed (via `cryptography`, already installed — not PyNaCl). The
signature covers the canonical JSON of every field except `signature` itself.

```json
{
  "grant_id": "gnt_...", "principal": "user_daksh", "agent_id": "agt_shopper_01",
  "limits": {
    "per_transaction_paise": 500000, "total_paise": 2000000, "spent_paise": 0,
    "max_transactions_per_day": 5,
    "velocity_window_seconds": 3600, "max_transactions_per_window": 2
  },
  "allow": { "merchants": ["mrc_demo_store"], "categories": ["groceries", "software"] },
  "deny":  { "categories": ["gambling"] },
  "step_up_above_paise": 200000,
  "not_before": "...", "expires_at": "...", "revoked": false,
  "signature": "ed25519:..."
}
```

All money in **paise, as integers**. Never floats — floating-point rupees is
how you ship a rounding bug into a payment system.

## The ten checks

Run **in this order, all of them, every time.** Never short-circuit: a trace
that stops at the first failure is a useless audit record, and the judges
asked for *explainable*.

| # | Check | Fails when |
|---|---|---|
| 1 | `GRANT_SIGNATURE` | Signature doesn't verify against the issuer public key |
| 2 | `GRANT_WINDOW` | `now < not_before` or `now > expires_at` |
| 3 | `GRANT_REVOKED` | `revoked` is true |
| 4 | `AGENT_MATCH` | Requesting agent ≠ `agent_id` |
| 5 | `MERCHANT_ALLOWED` | Merchant not in `allow.merchants` |
| 6 | `CATEGORY_ALLOWED` | Category in `deny`, or absent from `allow` |
| 7 | `PER_TXN_LIMIT` | `amount > per_transaction_paise` |
| 8 | `BUDGET_REMAINING` | `spent + amount > total_paise` |
| 9 | `VELOCITY` | Too many in the rolling window, or over the daily count |
| 10 | `IDEMPOTENCY` | This `request_id` was already executed |

## Decision

```
DENY     ← any check failed
STEP_UP  ← all passed, but amount > step_up_above_paise  (human approval)
ALLOW    ← all passed, under the step-up threshold
```

Output carries the outcome, **every** check with its pass/fail and the values
compared, and the *binding* check — the one that decided it. That triple is
what makes a money action "explainable" in the track's sense.

## Hard rules

- **No LLM anywhere in this path.** Not for classification, not for edge
  cases, not "just to summarise." This is the single most important rule in
  the project — see [`ambit-pipeline`](../ambit-pipeline/SKILL.md).
- **Record before attempting.** The decision is appended to the audit chain
  *before* the Razorpay call, so a crash mid-flight still leaves evidence.
- **Budget debits on `payment.captured`, not on ALLOW.** An allowed purchase
  that never gets paid must not consume budget.
- **`IDEMPOTENCY` is a replay defence.** Same `request_id` → return the
  original decision verbatim, never re-execute.
- **Fail closed.** Any unexpected error → `DENY`, logged. Never fail open.
- **Clock skew:** compare in UTC. Grants near expiry are denied, not rounded.

## Testing

Property-based (`hypothesis` if available, else generated cases): no
combination of inputs may produce `ALLOW` when any check fails. Plus explicit
cases for each of the ten reason codes, replay, revoke-mid-flight, and
boundary amounts (exactly at cap, one paise over).

Target is 100% decision accuracy on the labelled scenario suite — not
aspiration but definition: the layer is deterministic, so anything less is a
bug.
