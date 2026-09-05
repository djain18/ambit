---
name: self-anneal
description: Retry thresholds, fallback order, escalation conditions and error classification for Ambit's three independent external dependencies. Use when handling any failure from the Razorpay API, the Anthropic API, or the webhook tunnel — or when deciding whether a failure is transient, permanent, or needs a human.
---

# Self-annealing — failure handling across external dependencies

Required by `UNIVERSAL-CLAUDE.md` Playbook 2: a build with more than one
external dependency that can fail independently gets an explicit skill for it,
rather than retry logic scattered through the code.

Ambit has three:

| Dependency | Used by | Fails independently as |
|---|---|---|
| **Razorpay API** | OPEN, BOUND | 5xx, rate limit, timeout, business-rule rejection |
| **Anthropic API** | Buyer agent only | 429, overload, timeout, refusal |
| **Webhook tunnel** (ngrok/cloudflared) | OPEN fulfilment | Tunnel down, URL rotated, signature mismatch |

## Error classification — decide this first

Every failure gets exactly one class. The class determines the response.

| Class | Meaning | Response |
|---|---|---|
| `TRANSIENT` | Would likely succeed if retried (5xx, timeout, 429) | Retry with backoff |
| `PERMANENT` | Will never succeed as-is (4xx business rule, bad params) | Do not retry. Escalate or fail |
| `POLICY` | Deliberately refused by BOUND | **Never retry.** Not a failure — a correct outcome |
| `AMBIGUOUS` | Cannot be classified from the error alone | Treat as `PERMANENT`, flag for human |

**`POLICY` is not an error.** A denied purchase is the system working. Retrying
a policy denial would be a security bug, not resilience.

## Retry thresholds

| Dependency | Max attempts | Backoff | Then |
|---|---|---|---|
| Razorpay read | 3 | 1s, 2s, 4s + jitter | Escalate |
| Razorpay write | **2** | 2s, 6s + jitter | Escalate — never blind-retry a money write |
| Anthropic | 3 | 2s, 4s, 8s | Buyer agent gives up; demo continues |
| Webhook receipt | n/a | Razorpay retries on its own schedule | Reconcile by polling order status |

**Every money-moving write carries an idempotency key.** A retry must never
create a second order. This is why `IDEMPOTENCY` is one of the ten BOUND
checks rather than an afterthought.

## Stopping rules

Required by Track 01's bar (*"stopping rules"* are named explicitly).

- **Max 2 automated recovery attempts** on a failed purchase, then escalate to
  a human. No exceptions, no "one more try."
- **Escalation is a terminal state**, not a pause. Nothing auto-resumes after
  a human is involved.
- **A `STEP_UP` never auto-resolves.** It waits for a human or it expires.

## Fallback order

1. **Razorpay API unreachable** → serve OPEN's catalog from cache, refuse
   checkout with a clear reason. Degrade to read-only rather than lying about
   payment state.
2. **Webhook tunnel down** → poll order status on an interval to reconcile.
   Slower, correct, and the gap is visible in EXPLAIN.
3. **Anthropic unavailable** → the buyer agent stops. **Ambit itself keeps
   working** — the storefront and the checks are unaffected, which is the
   whole point of keeping the model outside the money path. Worth saying out
   loud in the pitch video.

## When a failure reveals a permanent constraint

Patch the code, then **update the relevant `SKILL.md`** so the next session
doesn't rediscover it — and add an entry to `docs/ENGINEERING-LOG.md` the same
day. A constraint learned and not written down gets paid for twice.

## Never

- Retry a `POLICY` denial.
- Retry a money write without an idempotency key.
- Fail open. Any unclassifiable error in the BOUND path is a `DENY`.
- Swallow an error silently. Every failure lands in the audit chain with its
  classification, including ones that were recovered.
