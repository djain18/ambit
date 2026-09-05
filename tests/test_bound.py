"""BOUND: one test per reason code, plus replay, revoke-mid-flight, boundaries.

The claim being tested is narrow and total: the decision layer is
deterministic, so any wrong answer is a bug rather than variance.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from ambit.bound.checks import CHECK_ORDER
from ambit.bound.decide import ALLOW, DENY, STEP_UP
from ambit.bound.grant import Limits
from tests.conftest import BASE_NOW, make_request


def resign(grant, keys, **changes):
    """Apply changes to a grant and sign it again, so it stays valid."""
    private, _ = keys
    unsigned = dataclasses.replace(grant, signature=None, **changes)
    return unsigned.signed(private)


def codes(decision):
    return [c.code for c in decision.checks]


# -- the shape of every trace -------------------------------------------
def test_all_ten_checks_run_in_fixed_order_on_a_clean_allow(engine, grant):
    decision = engine.decide(grant, make_request())
    assert decision.outcome == ALLOW
    assert tuple(codes(decision)) == CHECK_ORDER


def test_trace_is_complete_even_when_the_first_check_fails(engine, grant, keys):
    """No short-circuiting. A partial trace is a useless audit record."""
    tampered = dataclasses.replace(grant, step_up_above_paise=999_999_999)
    decision = engine.decide(tampered, make_request())
    assert decision.outcome == DENY
    assert decision.binding_check == "GRANT_SIGNATURE"
    assert tuple(codes(decision)) == CHECK_ORDER
    assert len(decision.checks) == 10


# -- one case per reason code -------------------------------------------
def test_grant_signature(engine, grant):
    tampered = dataclasses.replace(grant, step_up_above_paise=999_999_999)
    decision = engine.decide(tampered, make_request())
    assert (decision.outcome, decision.binding_check) == (DENY, "GRANT_SIGNATURE")


def test_grant_window_expired(engine, grant):
    decision = engine.decide(grant, make_request(now=BASE_NOW + timedelta(days=8)))
    assert (decision.outcome, decision.binding_check) == (DENY, "GRANT_WINDOW")


def test_grant_window_not_yet_valid(engine, grant):
    decision = engine.decide(grant, make_request(now=BASE_NOW - timedelta(days=2)))
    assert (decision.outcome, decision.binding_check) == (DENY, "GRANT_WINDOW")


def test_grant_revoked(engine, grant):
    engine.revoke(grant.grant_id, "user pressed revoke in the console")
    decision = engine.decide(grant, make_request())
    assert (decision.outcome, decision.binding_check) == (DENY, "GRANT_REVOKED")


def test_agent_match(engine, grant):
    decision = engine.decide(grant, make_request(agent_id="agt_somebody_else"))
    assert (decision.outcome, decision.binding_check) == (DENY, "AGENT_MATCH")


def test_merchant_not_on_allow_list(engine, grant):
    decision = engine.decide(grant, make_request(merchant_id="mrc_unknown"))
    assert (decision.outcome, decision.binding_check) == (DENY, "MERCHANT_ALLOWED")


def test_merchant_on_deny_list(engine, grant):
    decision = engine.decide(grant, make_request(merchant_id="mrc_blocked_store"))
    assert (decision.outcome, decision.binding_check) == (DENY, "MERCHANT_ALLOWED")


def test_category_on_deny_list(engine, grant):
    decision = engine.decide(grant, make_request(category="gambling"))
    assert (decision.outcome, decision.binding_check) == (DENY, "CATEGORY_ALLOWED")


def test_category_not_on_allow_list(engine, grant):
    decision = engine.decide(grant, make_request(category="fireworks"))
    assert (decision.outcome, decision.binding_check) == (DENY, "CATEGORY_ALLOWED")


def test_per_txn_limit(engine, grant):
    decision = engine.decide(grant, make_request(amount_paise=500_001))
    assert (decision.outcome, decision.binding_check) == (DENY, "PER_TXN_LIMIT")


def test_per_txn_rejects_zero_and_negative(engine, grant):
    for amount in (0, -100):
        decision = engine.decide(
            grant, make_request(request_id=f"req_{amount}", amount_paise=amount)
        )
        assert (decision.outcome, decision.binding_check) == (DENY, "PER_TXN_LIMIT")


def test_budget_remaining(engine, grant, keys):
    small = resign(
        grant,
        keys,
        limits=dataclasses.replace(grant.limits, total_paise=150_000),
    )
    decision = engine.decide(small, make_request(amount_paise=160_000))
    assert (decision.outcome, decision.binding_check) == (DENY, "BUDGET_REMAINING")


def test_velocity_window(engine, grant):
    for i in range(2):
        d = engine.decide(grant, make_request(request_id=f"req_v{i}", amount_paise=10_000))
        assert d.outcome == ALLOW
    third = engine.decide(grant, make_request(request_id="req_v2", amount_paise=10_000))
    assert (third.outcome, third.binding_check) == (DENY, "VELOCITY")


def test_velocity_daily_count(engine, grant, keys):
    """Window allows plenty; the daily cap is what bites."""
    daily = resign(
        grant,
        keys,
        limits=dataclasses.replace(
            grant.limits,
            max_transactions_per_window=99,
            velocity_window_seconds=1,
            max_transactions_per_day=2,
        ),
    )
    for i in range(2):
        d = engine.decide(
            daily,
            make_request(
                request_id=f"req_d{i}",
                amount_paise=10_000,
                now=BASE_NOW + timedelta(minutes=10 * i),
            ),
        )
        assert d.outcome == ALLOW
    third = engine.decide(
        daily,
        make_request(request_id="req_d2", amount_paise=10_000, now=BASE_NOW + timedelta(hours=3)),
    )
    assert (third.outcome, third.binding_check) == (DENY, "VELOCITY")


def test_idempotency_replay_with_altered_amount_is_denied(engine, grant):
    first = engine.decide(grant, make_request(request_id="req_same", amount_paise=10_000))
    assert first.outcome == ALLOW
    second = engine.decide(grant, make_request(request_id="req_same", amount_paise=400_000))
    assert (second.outcome, second.binding_check) == (DENY, "IDEMPOTENCY")


# -- replay, revoke mid-flight, step-up ----------------------------------
def test_honest_retry_returns_the_original_decision_verbatim(engine, grant, store):
    first = engine.decide(grant, make_request(request_id="req_retry", amount_paise=10_000))
    again = engine.decide(grant, make_request(request_id="req_retry", amount_paise=10_000))
    assert again.outcome == first.outcome
    assert again.replay_of == "req_retry"
    # and it must not have booked a second transaction
    assert len(store.ledger_for(grant.grant_id)) == 1


def test_revoke_mid_flight_stops_the_next_purchase(engine, grant):
    first = engine.decide(grant, make_request(request_id="req_a", amount_paise=10_000))
    assert first.outcome == ALLOW
    engine.revoke(grant.grant_id, "revoked live, mid-demo")
    second = engine.decide(grant, make_request(request_id="req_b", amount_paise=10_000))
    assert (second.outcome, second.binding_check) == (DENY, "GRANT_REVOKED")


def test_step_up_above_threshold(engine, grant):
    decision = engine.decide(grant, make_request(amount_paise=200_001))
    assert (decision.outcome, decision.binding_check) == (STEP_UP, "STEP_UP_THRESHOLD")
    assert all(c.passed for c in decision.checks)


def test_step_up_approval_re_runs_every_check(engine, grant):
    req = make_request(request_id="req_su", amount_paise=300_000)
    assert engine.decide(grant, req).outcome == STEP_UP
    approved = engine.approve_step_up(grant, req, approver="user_daksh")
    assert approved.outcome == ALLOW


def test_step_up_approval_cannot_rescue_a_revoked_grant(engine, grant):
    req = make_request(request_id="req_su2", amount_paise=300_000)
    assert engine.decide(grant, req).outcome == STEP_UP
    engine.revoke(grant.grant_id, "revoked while the human was deciding")
    approved = engine.approve_step_up(grant, req, approver="user_daksh")
    assert (approved.outcome, approved.binding_check) == (DENY, "GRANT_REVOKED")


# -- boundaries ----------------------------------------------------------
def test_exactly_at_the_per_transaction_cap_is_not_over_it(engine, grant):
    decision = engine.decide(grant, make_request(amount_paise=500_000))
    per_txn = next(c for c in decision.checks if c.code == "PER_TXN_LIMIT")
    assert per_txn.passed
    # 5,000 rupees is over the step-up threshold, so a human still signs off
    assert decision.outcome == STEP_UP


def test_one_paise_over_the_cap_is_denied(engine, grant):
    decision = engine.decide(grant, make_request(amount_paise=500_001))
    assert (decision.outcome, decision.binding_check) == (DENY, "PER_TXN_LIMIT")


def test_exactly_at_the_step_up_threshold_is_allowed(engine, grant):
    decision = engine.decide(grant, make_request(amount_paise=200_000))
    assert decision.outcome == ALLOW


def test_budget_boundary_exact_fit_then_one_paise_over(engine, grant, keys):
    tight = resign(
        grant, keys, limits=dataclasses.replace(grant.limits, total_paise=100_000)
    )
    exact = engine.decide(tight, make_request(request_id="req_fit", amount_paise=100_000))
    assert exact.outcome == ALLOW
    over = engine.decide(tight, make_request(request_id="req_over", amount_paise=1))
    assert (over.outcome, over.binding_check) == (DENY, "BUDGET_REMAINING")


# -- money movement ------------------------------------------------------
def test_budget_debits_on_capture_not_on_allow(engine, grant, store):
    engine.decide(grant, make_request(request_id="req_cap", amount_paise=50_000))
    assert store.captured_paise(grant.grant_id) == 0
    engine.record_capture("req_cap", "pay_test123", 50_000)
    assert store.captured_paise(grant.grant_id) == 50_000


def test_an_allow_that_never_pays_stops_holding_budget(engine, grant, store):
    engine.decide(grant, make_request(request_id="req_ghost", amount_paise=50_000))
    assert store.in_flight_paise(grant.grant_id, BASE_NOW) == 50_000
    later = BASE_NOW + timedelta(hours=2)
    assert store.in_flight_paise(grant.grant_id, later) == 0


# -- fail closed ---------------------------------------------------------
def test_a_broken_grant_denies_rather_than_crashing(engine, grant, keys):
    broken = resign(grant, keys, expires_at="not-a-timestamp")
    decision = engine.decide(broken, make_request())
    assert (decision.outcome, decision.binding_check) == (DENY, "INTERNAL_ERROR")


def test_no_public_key_means_deny(store, chain, grant):
    from ambit.bound.decide import BoundEngine

    blind = BoundEngine(store, chain, public_key=None)
    decision = blind.decide(grant, make_request())
    assert (decision.outcome, decision.binding_check) == (DENY, "GRANT_SIGNATURE")


# -- the audit chain -----------------------------------------------------
def test_every_decision_is_recorded_before_it_is_returned(engine, grant, chain):
    engine.decide(grant, make_request(request_id="req_chain", amount_paise=10_000))
    entries = chain.entries()
    assert [e["type"] for e in entries] == ["DECISION"]
    assert chain.verify().ok


def test_chain_verification_fails_loudly_on_an_edited_row(engine, grant, chain):
    import json

    engine.decide(grant, make_request(request_id="req_t1", amount_paise=10_000))
    engine.decide(grant, make_request(request_id="req_t2", amount_paise=20_000))
    assert chain.verify().ok

    rows = [json.loads(line) for line in chain.path.read_text(encoding="utf-8").splitlines()]
    rows[0]["payload"]["outcome"] = "ALLOW_FORGED"
    chain.path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )

    result = chain.verify()
    assert not result.ok
    assert result.broken_at_seq == 0


@pytest.mark.parametrize("outcome", [ALLOW, STEP_UP, DENY])
def test_every_outcome_is_one_of_the_three(engine, grant, outcome):
    cases = {
        ALLOW: make_request(request_id="req_o1", amount_paise=10_000),
        STEP_UP: make_request(request_id="req_o2", amount_paise=300_000),
        DENY: make_request(request_id="req_o3", amount_paise=900_000),
    }
    assert engine.decide(grant, cases[outcome]).outcome == outcome
