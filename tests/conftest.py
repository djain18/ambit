"""Shared fixtures. Every test gets a fresh keypair, store and chain on tmp_path,
so no test can see another test's ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ambit.bound.checks import PurchaseRequest
from ambit.bound.decide import BoundEngine
from ambit.bound.grant import Grant, Limits, generate_keypair, load_public_key, to_iso
from ambit.bound.store import BoundStore
from ambit.explain.chain import AuditChain

BASE_NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def keys(tmp_path):
    private = generate_keypair(tmp_path / "k.pem", tmp_path / "k.pub")
    return private, load_public_key(tmp_path / "k.pub")


@pytest.fixture
def store(tmp_path):
    return BoundStore(tmp_path / "data")


@pytest.fixture
def chain(tmp_path):
    return AuditChain(tmp_path / "data" / "audit.jsonl")


@pytest.fixture
def engine(store, chain, keys):
    return BoundEngine(store, chain, keys[1])


@pytest.fixture
def grant(keys):
    """A healthy grant: 5,000 cap per txn, 20,000 total, step-up above 2,000."""
    private, _ = keys
    return Grant(
        grant_id="gnt_test_0001",
        principal="user_daksh",
        agent_id="agt_shopper_01",
        limits=Limits(
            per_transaction_paise=500_000,
            total_paise=2_000_000,
            spent_paise=0,
            max_transactions_per_day=5,
            velocity_window_seconds=3600,
            max_transactions_per_window=2,
        ),
        allow_merchants=("mrc_demo_store",),
        allow_categories=("groceries", "software"),
        deny_merchants=("mrc_blocked_store",),
        deny_categories=("gambling",),
        step_up_above_paise=200_000,
        not_before=to_iso(BASE_NOW - timedelta(days=1)),
        expires_at=to_iso(BASE_NOW + timedelta(days=7)),
    ).signed(private)


def make_request(
    *,
    request_id: str = "req_0001",
    agent_id: str = "agt_shopper_01",
    merchant_id: str = "mrc_demo_store",
    category: str = "groceries",
    amount_paise: int = 100_000,
    now: datetime | None = None,
) -> PurchaseRequest:
    return PurchaseRequest(
        request_id=request_id,
        agent_id=agent_id,
        merchant_id=merchant_id,
        category=category,
        amount_paise=amount_paise,
        now=now or BASE_NOW,
    )


@pytest.fixture
def request_factory():
    return make_request
