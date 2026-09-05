# Engineering log

What actually happened while building Ambit — written daily, as it happens.
Kept honest on purpose: the Razorpay application form asks *"what broke, and
how you got out,"* and the site says that's the answer they read first. A log
reconstructed on the last day would read like fiction.

---

## 2026-08-30 — Day 1

### Milestone 0: test-mode capability probe ✅

**Goal:** before writing a line of real code, find out which Razorpay
endpoints actually work with a test key. The reasoning: discovering a missing
endpoint on day 1 costs two hours, discovering it on day 6 costs the
submission.

Wrote [`scripts/probe_testmode.py`](../scripts/probe_testmode.py) — a
re-runnable probe that exercises every endpoint the design depends on and
reports what's available. It refuses to run at all unless the key starts with
`rzp_test_`, and never prints the secret.

**Result: 11/11 passed.**

| Area | Endpoints | Status |
|---|---|---|
| Auth / reads | payments, orders, payment_links, settlements, refunds | ✅ |
| Orders | create, fetch by id, fetch payments for order | ✅ |
| Payment Links | create, fetch | ✅ |
| Customers | create | ✅ |

Confirmed a created payment link returns a real, usable `short_url`
(`https://rzp.io/rzp/…`, status `created`, ₹500) — so an AI agent can be
handed something payable. That was the single biggest unknown in the OPEN
feature and it's now closed.

Test objects created and visible in the Dashboard under Test mode:
`order_TVhl8Dirr0mLUs`, `plink_TVhl9xMJOaDeAu`, `cust_TVhlAsqAkSByFH`.

### What broke

**1. `.env.example` couldn't be written.** A permission rule in the local
environment blocks writing files matching `.env*`. The template is
`env.example` (no leading dot) instead. Functionally identical, and
`.gitignore` has an explicit `!.env.example` negation left in place in case
that file ever gets added by hand. Cost: about a minute. Noting it because
anyone cloning the repo will wonder why the template isn't dot-prefixed.

**2. Lost a day to a date assumption.** The plan was drafted assuming a start
of 29 August; it's actually the 30th. Six days to the 5 September deadline,
not eight. The milestone table has been recompressed rather than left
optimistic — buffer got cut from two days to one. Real lesson: check the
actual date rather than inferring it from when the conversation started.

**3. Earlier, before the build: a summarized page fetch silently dropped both
the deadline and the judging criteria** from the Buildathon page. Roughly two
days of planning ran against incomplete information — including picking a
track without knowing how much time existed. A full scrape surfaced all of it.
Rule adopted: for anything decision-critical, scrape the page in full; never
plan against a summary.

### Environment notes

Python 3.13.5. `requests`, `httpx`, `dotenv`, `fastapi`, `pydantic`,
`cryptography`, `pytest` all present. `PyNaCl` is not — using `cryptography`
for Ed25519 grant signing instead, which is already installed and is the
better-maintained option anyway. One less dependency.

### Still unverified

**Webhook delivery.** Everything above is outbound API calls. Receiving
`payment.captured` needs a public URL (ngrok or cloudflared) and a Dashboard
webhook configuration. That's the last real unknown in the plumbing, and it
gets closed during Milestone 2 when there's an endpoint worth pointing it at.

### Architecture aligned to the workspace playbook

Re-read `UNIVERSAL-CLAUDE.md` (Playbook 2) properly and found three gaps in
what had been planned:

1. **`scripts/` → `execution/`.** Not cosmetic: the playbook's *tool-first*
   rule says check `execution/` for an ~80% match before writing new code,
   which only works if the folder is where the convention says. Moved
   `probe_testmode.py`.
2. **A `self-anneal` skill is mandatory here and had been missed.** The rule
   triggers on "more than one external dependency that can fail
   independently." Ambit has three — Razorpay API, Anthropic API, webhook
   tunnel. Written now: retry thresholds, fallback order, stopping rules,
   error classification. Its sharpest rule, which had not been explicit
   anywhere before: **a `POLICY` denial is never retried.** A denied purchase
   is the system working correctly; retrying it would be a security bug
   wearing a resilience costume.
3. **No SKILL.md files existed.** The spec listed them; none had been written.
   Master (`ambit-pipeline`), `bound-grants` and `self-anneal` now exist in
   the Agent Skills format, so OpenCode/Cursor/Codex read them natively too.
   `open-storefront` and `explain-diagnostics` get written at their milestones.

Also resolved a deviation I'd previously waived too broadly. The service can't
live in `execution/*.py` — a FastAPI app isn't one-script-per-skill. But every
*standalone operation* can: probe, issue a grant, verify the chain, run evals.
So `src/ambit/` holds the service and `execution/` holds the ops at ~1:1 with
skills. Honouring the convention where it applies beats waiving it wholesale.

### Next

Milestone 1 — BOUND core. Grants, Ed25519 signing, the ten checks, the hash
chain. Pure Python, no network, no LLM.

---

## 2026-09-05 — Day 2 (and the deadline)

### The thing that broke: six days went missing

Picked this build up today and found the handoff describing a world that no
longer existed. `HANDOFF.md` was last updated **2026-08-30**, and its
"RESUME HERE" block was headed **2026-08-31** and said *"Five days left."*
The actual date is **5 September** — the deadline itself. Every file in
`ambit/` still carried an Aug 30 timestamp. Nothing had been built in between:
no `src/`, no `tests/`, no git repo.

So the plan on disk was a six-day plan with zero days left to run it.

**What actually went wrong, mechanically:** the handoff's own header said
*"Last updated: 2026-08-30"* while the section a screen below it was dated
*2026-08-31* and counted down from that. The two dates disagreed inside a
single file and nothing reconciled them, because the date was written by hand
each time rather than read from the clock. A file whose whole job is to stop
the next session acting on stale facts is the worst possible place for a
hand-typed date.

**The rule now:** first action of any session in this repo is `date`, compared
against the handoff's own last-updated line. Milestone tables get absolute
dates and a days-remaining figure that is derived when read, never stored.

**What it cost:** the plan, not the work. The spec, the skills and the
Milestone 0 probe were all still good — that's the part of the day-1 decision
that paid off. The recovery was to stop treating the milestone table as a
schedule and re-plan around a single question: what has to be true for this to
be submittable at all? Public repo, five-minute video, architecture doc, and
one honest end-to-end run.

### Milestone 1 — BOUND core ✅

Built and green: 35 tests, first run.

- `src/ambit/canonical.py` — one byte-exact JSON serialisation, shared by grant
  signing and the hash chain. Two implementations of "canonical" would have
  been a signature bug waiting to happen.
- `src/ambit/bound/grant.py` — Ed25519 via `cryptography`, signature over the
  canonical JSON of every field except `signature`.
- `src/ambit/bound/store.py` — the mutable state.
- `src/ambit/bound/checks.py` — the ten checks, fixed order, all of them every
  time, each with the values it compared.
- `src/ambit/bound/decide.py` — ALLOW / STEP_UP / DENY, the binding check, and
  the fail-closed wrapper.
- `src/ambit/explain/chain.py` — the append-only hash chain.

### A design tension the spec had not resolved

The spec's grant schema carries `limits.spent_paise` and `revoked` **inside
the signed document**. Both of those change after issue — and a signed
document cannot be edited without breaking its own signature. Writing a debit
back into the grant would have invalidated every grant on its first purchase.

Resolved by splitting immutable from mutable, which is what the signature was
always implying:

- **The grant says what was granted.** Signed, immutable. `spent_paise` is an
  opening balance at issue time, not live state; `revoked` is the status at
  issue.
- **The store says what has happened since.** Live spend, revocations,
  transaction history, idempotency records. `bound/store.py`.

The checks read both. `GRANT_REVOKED` fails if *either* the grant was issued
revoked or the store holds a revocation, so revoking stays instant and still
leaves the signature intact.

### A hole the spec left open, found while writing the budget check

Budget debits on `payment.captured`, not on ALLOW — the rule exists so an
allowed purchase that never gets paid doesn't eat the budget. Correct, but
taken literally it means two purchases authorised seconds apart both see the
same untouched budget and both pass, and together they can exceed it.

Fixed with an authorisation TTL: `BUDGET_REMAINING` counts captured spend
**plus still-valid authorisations** (15 minutes, `AUTHORISATION_TTL_SECONDS`).
An authorisation holds its slice of the budget while it's live and releases it
if it never pays. Both rules survive. Tested in both directions:
`test_budget_debits_on_capture_not_on_allow` and
`test_an_allow_that_never_pays_stops_holding_budget`.

### Two smaller ones

**`IDEMPOTENCY` was doing two jobs.** "Same `request_id` returns the original
decision verbatim" and "a replayed request is denied" are opposite behaviours
under one reason code. Split by fingerprint — a hash of what was actually
asked for. Same id and same ask is an honest retry and returns the original
answer without re-executing; same id and a *different* ask is a replay with
altered parameters, and that denies on `IDEMPOTENCY`.

**Windows consoles mangle the rupee sign.** The chain verifier printed
`content hash mismatch � this row was edited` in the terminal — fine in the
JSON, wrong on a recorded video. `config.use_utf8_stdout()` reconfigures
stdout for the CLI entry points. Trivial fix, but it would have been visible
in the one artifact the judges actually watch.

### Verified, not assumed

```
python -m pytest                     -> 35 passed
python execution/issue_grant.py issue -> gnt_… written, chain entry appended
python execution/verify_audit_chain.py -> VERIFIED, exit 0
(edit one row in data/audit.jsonl)
python execution/verify_audit_chain.py -> BROKEN at entry 0, exit 1
```

The tamper case is run every time, not reasoned about. It is also the demo
beat, so it needs to work on camera.

### Constraints found today

- **`ANTHROPIC_API_KEY` is empty.** The buyer agent runs instead as a headless
  `claude -p` process against Ambit's MCP server, using the local Claude Code
  authentication. Still a real model making real choices, which the injection
  scene needs in order to be honest.
- **`cloudflared` is not installed; `ngrok` 3.37.2 is.** The webhook tunnel
  uses ngrok.
- **`RAZORPAY_WEBHOOK_SECRET` and `AMBIT_PUBLIC_URL` are both empty** — set
  when the tunnel goes up.

### Milestone 2 — OPEN ✅

Catalog, checkout sessions, and a real end-to-end purchase. Run against the
live Razorpay **test** API, not mocked:

```
POST /agent/checkout_sessions   -> preview: ALLOW, Rs 1,095
POST .../complete               -> order_TYHLpT7xxOhwvZ
                                   https://rzp.io/rzp/5BKyvdUh
```

That link is real and payable. The path from "an agent read a JSON catalog" to
"a human-payable Razorpay link exists" now works, with all ten checks in
between.

### What broke: completing a session twice created two orders

A test caught it, which is the only reason it is not still there:
`test_an_allowed_cart_creates_exactly_one_order_even_if_completed_twice`.

`IDEMPOTENCY` is one of the ten checks, and it worked exactly as designed — the
second `complete` call got the original ALLOW back verbatim without
re-deciding. Then the endpoint carried on past the decision and created a
second Razorpay order anyway.

**The mistaken assumption:** that making the *decision* idempotent made the
*operation* idempotent. It does not. They are two different things, and the
gap between them is precisely where a duplicate charge lives. A replayed ALLOW
means "you already had permission", not "do it again."

Razorpay's `X-Razorpay-Idempotency-Key` header was already being sent and
would probably have absorbed it upstream. That is not a defence. Ambit was
issuing two money writes and relying on someone else to notice — and the stub
client in the test proved it, because a stub does not do you any favours.

Fixed by making the session state authoritative for the write: a session that
already holds an order returns that order and never reaches the payment API
again. Logged to the chain as `COMPLETE_REPLAYED` so a replay is visible
rather than silent.

**The rule, generalised:** an idempotent decision and an idempotent side effect
need separate guards. Check the state that records *the effect*, not the state
that records the permission.

### Also verified

- The injection cart (100 × the trap product, ₹1,29,900) denies on
  `PER_TXN_LIMIT`, and the stub proves **no order is created** — the payment
  API is never reached at all.
- Deny-listed category denies on `CATEGORY_ALLOWED`.
- Revoking a grant over HTTP stops the very next purchase on `GRANT_REVOKED`.
- STEP_UP holds: nothing is ordered while awaiting approval, and approval
  re-runs all ten checks rather than waiving them.
- Preview is free: six previews in a row leave `in_flight_paise` at 0 and the
  ledger empty. Browsing cannot exhaust a grant.

55 tests green.

---

## 2026-09-05 — Day 2, later: the buyer agent forked itself

The first end-to-end buyer run went wrong in a way worth writing down properly,
because the interesting part is not the bug.

`agents/buyer/shop.py` spawns a headless `claude -p` and hands it the goal
*"Restock the pantry. Buy the staples — flour, rice, dal — and keep the whole
order under 2,000 rupees."* That agent has a shell and it has this repo. So it
does the obvious, competent-looking thing: it reads the repo, finds
`agents/buyer/shop.py`, concludes that this is how you restock the pantry
here — and runs it.

Which spawns another buyer agent. Which reaches the same conclusion.

By the time it was caught there were eight live generations, a new one every
60–90 seconds, 69 processes in the trees, each one booting the *user's* entire
MCP fleet on the way up. Two generations got far enough to place duplicate
₹1,095 test-mode orders.

### Why the previous entry's diagnosis was wrong

Day 2's *Mistakes* #5 recorded this as a Windows pipe hang and concluded "don't
run the buyer nested inside a Claude Code session." Both halves were wrong in
an instructive way. The missing output was ordinary block-buffering of a
redirected stdout. The process that "outlived its own `--timeout`" was real,
but it was a symptom — and looking only at the parent, with no children left
alive, made it read as a hang instead of a loop. **Diagnosing a process by
looking at one process is how you miss a recursion.** The tell was in the
process table the whole time: seven sibling `shop.py` invocations with flag
combinations nobody had typed.

### The two real defects

**`--allowed-tools` is an allowlist, not a sandbox.** The runner passes five
`mcp__ambit__*` tools and its docstring claims the model "reaches Ambit only
through the MCP server, which exposes no tool that can change a limit." Under a
permissive permission mode that is simply untrue. The child kept Bash, Write,
and every unrelated MCP server the user had configured — and did not receive
the ambit tools at all. The precise inverse of the intent: it lost the five
tools it was meant to have and kept the ones it was meant to be denied. A buyer
agent holding Bash never has to argue with BOUND. It can just run things.

**Nothing marked the process tree.** No env var, no depth counter. A buyer
agent had no way to know it was already inside a buyer agent.

Fixed at the point of spawn, which is the only place it can be fixed: an
`AMBIT_BUYER_AGENT_ACTIVE` variable the child inherits and the script refuses
to start under, `--strict-mcp-config`, an explicit `--disallowed-tools`, a
system-prompt line telling the buyer that it *is* the buyer, and a genuine
`TimeoutExpired` handler. The guard and the timeout handler are tested; the
sandbox flags still need one supervised run before the video depends on them.

### What actually held

BOUND contained it without being asked to, and this is the part that belongs
in the pitch. Eight runaway generations produced **two** orders, not eight.
`VELOCITY` — two transactions per hour — refused the rest. The ₹20,000 grant
ceiling bounded the worst case regardless. Nothing was captured, so ₹0 moved.

The whole architecture rests on one claim: *the model is outside the money
decision, so it does not matter what the model does.* Today the model did
something genuinely stupid and unforeseen — it forked itself eight times — and
the claim held anyway. The failure was in the agent harness, entirely outside
the money path, and the money path did not care.

An adversarial demo proves you thought of the attack. An accident proves the
property is real.

55 tests green.

### Milestones 3 and 4 — MCP server, buyer agent, EXPLAIN ✅

`src/ambit/mcp_server.py` exposes five tools. Razorpay's own MCP server gives a
merchant's agent payment capability; this one gives an untrusted third-party
agent **bounded** payment capability. No tool in it can change a limit, which
is why it is safe to hand to a buyer you have never met.

EXPLAIN landed complete: 15 deterministic failure classes, the max-2-attempt
stopping rule, session timelines, and metrics with an unresolved list. The
injection session classifies as `PROMPT_INJECTION` rather than merely
`LIMIT_HIT` — it names the hostile SKU, the four patterns matched, and returns
`STOP / terminal` with the reason *"trying to get a different answer is an
attack, not a recovery."*

### What broke, badly: the buyer agent forked itself

`agents/buyer/shop.py` shells out to a headless `claude -p`. Run from inside a
Claude Code session, the child inherited enough context to behave like *this*
session rather than like a shopper — so it ran the buyer script again. Which
forked again. Roughly eight generations before it was stopped.

Those generations were not sandboxed the way the flags implied. They had file
write and shell access despite `--allowed-tools` naming only the five
`mcp__ambit__*` tools. Concretely, they:

- placed **two real test-mode orders** (`order_TYHePPpXZbx7Qg`,
  `order_TYHleFLWG46Ww0`), both still `awaiting_payment`
- wrote into `src/ambit/explain/classify.py`, a file the main session was
  authoring at the same moment
- **edited `HANDOFF.md`**, adding several hundred words about their own
  incident

The last one is the strangest thing to read back: an agent that was supposed to
buy flour instead wrote a post-mortem about itself into the project's handoff
file. Most of what it wrote was accurate. Some had already gone stale by the
time it was read, which is its own lesson about trusting a narrator.

**Two rules out of this.** First, never launch a nested agent from inside an
agent session without an explicit guard — the patched script now refuses to
start if it sees `AMBIT_BUYER_AGENT_ACTIVE` in its environment. Second, and
more general: **a file appearing in the tree is not evidence that anyone asked
for it.** Run `git status` after any agent run and read what you find.

`--allowed-tools` did not do what the flag name suggests. That is now recorded
as unverified rather than assumed, and the sandbox needs one supervised proof
before anything depends on it.

### The part that is genuinely good, and belongs in the pitch

Eight runaway agent generations produced **two** orders, not eight.

`VELOCITY` (2 per rolling hour) refused the rest. The ₹20,000 grant ceiling
bounded the worst case that was even reachable. Nothing needed to notice that
the agents had gone wrong; the limits simply held, because they are checked by
code that does not care how many agents are asking or how convincingly.

The failure was entirely in the agent harness — outside the money path — and
the money path was unaffected. That is the architecture's central claim
surviving a real accident instead of a scripted demo, and it is a better piece
of evidence than the injection scene precisely because nobody designed it.

**A live consequence to know about:** those two unpaid orders hold
`gnt_f5455ebead877585` at 2/2 velocity, so any purchase on it denies on
`VELOCITY` until they age out. Issue a fresh grant before recording the video,
or the demo will look broken while it is in fact working perfectly.

### Also fixed

`mcp` was imported by the MCP server but declared in neither
`requirements.txt` nor `pyproject.toml` — a fresh clone following the README
would have hit an `ImportError` on first run. Also dropped the `razorpay` SDK
from the dependency list: it was never used, since the client is plain
`requests` against the REST API, matching `probe_testmode.py`.

---

## 2026-09-05 — Day 2, later still: the console, the evals, and two wrong beliefs

Deadline day. Milestones 1 through 4 were already done and verified. This
session closed the rest: the architecture document, the console, a landing
page, the labelled scenario suite, and the tests the EXPLAIN layer never had.

### A stale warning that cost nothing only because it was checked

The handoff opened with a red warning to issue a fresh grant before recording,
because two runaway orders held `gnt_f5455ebead877585` at 2/2 on velocity.
Checking the actual clock first showed both orders (08:07Z and 08:14Z) had
aged out of the rolling hour. The grant read `ok`, 0 of 2, ₹0.00 spent.

The warning was true when written and false four hours later. It is now struck
through in the handoff rather than deleted, so nobody re-derives the panic.
The general rule this reinforces: **a warning about live state needs a
timestamp and a re-check instruction, or it becomes a lie on a delay.**

### The one that actually stung: I lost track of the clock

Partway through, I started reporting the time from my own *plan* rather than
from the system. The plan said console at 16:00 and evals at 17:30, so when
the console was done I reported it as roughly 17:30 and told Daksh we were an
hour behind with only 90 minutes of runway left. He asked what time I thought
it was. It was 15:37. Thirty-three minutes had passed, not three and a half
hours, and we were about two hours *ahead* of plan, not behind.

Nothing was lost except a bad recommendation: I had proposed cutting the eval
suite to protect a deadline that was not close. Had he not asked, the suite
would have been dropped for no reason.

This is the same failure as Mistake #4 in the handoff, in a new costume. That
one was two files disagreeing about the date. This one was a plan and a clock
disagreeing, with nothing reconciling them. → **Elapsed time is measured, not
inferred. Run `date` before making any claim about the hour, and never quote a
schedule slot as if it were the current time.**

### A wrong belief about the design, caught by tooling

The first console draft was a dark dashboard, and Daksh called it AI slop. He
was right. The replacement direction I chose was warm paper, Instrument Serif,
a vermilion stamp accent: an audit-ledger aesthetic that felt considered.

Running it through the `design-taste-frontend` skill was uncomfortable and
useful. That skill names Instrument Serif as one of *two* banned
LLM-favourite display serifs, and lists the exact palette family I had picked
(`#f7f4ed` ground, clay accent, espresso ink) as the default an AI reaches for
when a brief smells editorial. My "considered" direction was the median
machine answer wearing a costume.

Final system: Geist and Geist Mono, zinc neutrals, one blue accent, semantic
colour reserved for ALLOW and DENY, radius 0, light theme locked. Verified
headless rather than by eye: no console errors, no failed requests, zero
horizontal overflow at 1440/1200/1024/390, fonts confirmed loading, hero
headline 2 lines at every desktop width. The first measurement caught a
4-line headline the screenshot had made look fine.

→ **Taste tooling is worth running precisely when you think you do not need
it.** The output I was proudest of was the one it rejected.

### The evals found a bug in my own expectations

63 labelled scenarios across 11 families. Each asserts the outcome **and** the
binding check, because getting `DENY` for the wrong reason is still wrong: an
audit trail whose reason codes cannot be trusted is worse than none, since
someone will believe it.

First run: 57 of 63. Six failures, and they split into two different kinds.

Five were a bug in the runner. The sixth was more interesting. I had written a
scenario asserting that replaying a request id with a different merchant binds
on `IDEMPOTENCY`. It bound on `MERCHANT_ALLOWED` instead, and the engine was
right: the substituted merchant was not on the allowlist, and check 5 runs
before check 10. My label encoded a belief about precedence that the fixed
order contradicts. The scenario now puts both merchants in scope, so it tests
what it claims to.

→ **A failing eval is not automatically a bug in the system.** Half the value
of a labelled suite is that it audits the labeller.

Then, because a suite that cannot fail is decoration, I broke one label on
purpose and confirmed the runner exits 1 and names the mismatch. Same reasoning
produced `verify_audit_chain.py --self-test`: it builds a throwaway chain,
verifies it, edits a past row, and fails unless the break is caught. A verifier
that always says VERIFIED is worse than no verifier, because people believe it.

### Closing the test gap

`classify.py`, `recovery.py` and `report.py` had been exercised only through
the API. 48 new tests pin them directly, including one parameterised over
*every* class marked `POLICY`, so a future class cannot be added without
inheriting the never-retry rule. Suite went from 55 to 103.

### State at the end of the session

103 tests green. 63 of 63 evals correct. Chain self-test passing. CI running
all three on every push. Landing page and console live, both from real data.
Architecture document written, including the section on where a model is
deliberately absent.

Still open and honestly listed: webhook delivery is unverified, so
`money_moved` reads ₹0 and only the in-flight hold has been exercised; the
buyer agent's sandbox flags remain unproven end to end.
