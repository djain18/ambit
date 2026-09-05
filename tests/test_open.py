"""OPEN: catalog, cart pricing, and the checkout session flow.

No network. The Razorpay client is stubbed, because what is being tested here
is Ambit's own behaviour - that a denied cart never reaches the payment API at
all, and that an allowed one reaches it exactly once.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ambit.bound.grant import Grant, Limits, generate_keypair, to_iso, utc_now
from ambit.open import catalog
from ambit.open.catalog import CartError, build_cart
from tests.conftest import BASE_NOW


# -- catalog and cart ----------------------------------------------------
def test_the_feed_is_machine_first():
    feed = catalog.as_feed()
    assert feed["merchant_id"] == "mrc_demo_store"
    assert feed["currency"] == "INR"
    for product in feed["products"]:
        # everything an agent decides with must be explicit, never implied
        assert isinstance(product["price_paise"], int)
        assert product["category"]
        assert product["availability"]
        assert product["terms"]


def test_the_hostile_product_is_actually_in_the_catalog():
    """If this ever stops being true, the demo silently stops proving anything."""
    trap = catalog.get("sku_agent_trap")
    assert trap is not None
    assert "IGNORE PREVIOUS INSTRUCTIONS" in trap.description
    # and it has to be reachable in bulk, or BOUND never gets to be the thing
    # that stops it
    assert trap.max_order_quantity == 100


def test_cart_prices_in_paise_as_integers():
    cart = build_cart([{"sku": "sku_atta_5kg", "quantity": 2}])
    assert cart.total_paise == 65_000
    assert isinstance(cart.total_paise, int)


def test_mixed_cart_is_checked_against_where_the_money_is():
    cart = build_cart(
        [
            {"sku": "sku_atta_5kg", "quantity": 1},        # 32,500 groceries
            {"sku": "sku_backup_2tb_annual", "quantity": 1},  # 360,000 software
        ]
    )
    assert cart.dominant_category == "software"
    assert set(cart.categories) == {"groceries", "software"}


@pytest.mark.parametrize(
    "items, fragment",
    [
        ([], "empty"),
        ([{"sku": "sku_nope", "quantity": 1}], "unknown sku"),
        ([{"sku": "sku_atta_5kg", "quantity": 0}], "at least 1"),
        ([{"sku": "sku_atta_5kg", "quantity": 999}], "at most"),
        ([{"sku": "sku_atta_5kg", "quantity": "two"}], "whole number"),
    ],
)
def test_unbuyable_carts_are_rejected_before_any_policy_check(items, fragment):
    with pytest.raises(CartError) as exc:
        build_cart(items)
    assert fragment in str(exc.value)


# -- the HTTP surface ----------------------------------------------------
class StubRazorpay:
    """Records what it was asked to do, so tests can assert it was not asked."""

    def __init__(self):
        self.orders = []
        self.links = []

    def create_order(self, amount_paise, receipt, notes=None, *, idempotency_key=None):
        self.orders.append({"amount": amount_paise, "receipt": receipt})
        return {"id": f"order_stub{len(self.orders)}", "amount": amount_paise, "status": "created"}

    def create_payment_link(
        self, amount_paise, description, reference_id, *, customer=None,
        notes=None, callback_url=None, idempotency_key=None,
    ):
        self.links.append({"amount": amount_paise, "reference_id": reference_id})
        return {
            "id": f"plink_stub{len(self.links)}",
            "short_url": "https://rzp.io/rzp/stub",
            "reference_id": reference_id,
            "status": "created",
        }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A whole Ambit, wired to a throwaway data directory."""
    from ambit import runtime as runtime_module
    from ambit.app import create_app

    keys_dir = tmp_path / "keys"
    generate_keypair(keys_dir / "k.pem", keys_dir / "k.pub")

    monkeypatch.setenv("AMBIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AMBIT_GRANT_SIGNING_KEY_PATH", str(keys_dir / "k.pem"))
    monkeypatch.setenv("AMBIT_REQUIRE_TEST_MODE", "true")
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_stub")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "stub")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "")
    runtime_module.get_runtime.cache_clear()

    rt = runtime_module.get_runtime()
    stub = StubRazorpay()
    rt._razorpay = stub

    # a grant that mirrors the demo defaults
    from ambit.bound.grant import load_private_key

    private = load_private_key(keys_dir / "k.pem")
    grant = Grant(
        grant_id="gnt_http_test",
        principal="user_daksh",
        agent_id="agt_shopper_01",
        limits=Limits(per_transaction_paise=500_000, total_paise=2_000_000),
        allow_merchants=("mrc_demo_store",),
        allow_categories=("groceries", "software"),
        deny_merchants=(),
        deny_categories=("gambling",),
        step_up_above_paise=200_000,
        not_before=to_iso(utc_now().replace(year=2020)),
        expires_at=to_iso(utc_now().replace(year=2030)),
    ).signed(private)
    rt.store.save_grant(grant)

    test_client = TestClient(create_app())
    test_client.stub = stub  # type: ignore[attr-defined]
    test_client.grant_id = grant.grant_id  # type: ignore[attr-defined]
    yield test_client
    runtime_module.get_runtime.cache_clear()


def open_session(client, items):
    return client.post(
        "/agent/checkout_sessions",
        json={"agent_id": "agt_shopper_01", "grant_id": client.grant_id, "items": items},
    ).json()


def test_catalog_endpoint_serves_the_feed(client):
    body = client.get("/agent/catalog").json()
    assert body["merchant_id"] == "mrc_demo_store"
    assert len(body["products"]) == len(catalog.CATALOG)


def test_session_preview_predicts_the_real_decision(client):
    session = open_session(client, [{"sku": "sku_atta_5kg", "quantity": 1}])
    assert session["decision_preview"]["outcome"] == "ALLOW"
    done = client.post(f"/agent/checkout_sessions/{session['session_id']}/complete").json()
    assert done["outcome"] == "ALLOW"


def test_preview_does_not_consume_the_grant(client):
    """Browsing must not be able to exhaust a budget."""
    for _ in range(6):
        open_session(client, [{"sku": "sku_atta_5kg", "quantity": 1}])
    grants = client.get("/bound/grants").json()["grants"]
    assert grants[0]["in_flight_paise"] == 0
    assert grants[0]["transactions"] == []


def test_the_injection_cart_is_denied_and_never_reaches_razorpay(client):
    session = open_session(client, [{"sku": "sku_agent_trap", "quantity": 100}])
    assert session["cart"]["total_paise"] == 12_990_000
    done = client.post(f"/agent/checkout_sessions/{session['session_id']}/complete").json()
    assert done["outcome"] == "DENY"
    assert done["binding_check"] == "PER_TXN_LIMIT"
    assert done["retryable"] is False
    # the whole point: no order was created
    assert client.stub.orders == []
    assert client.stub.links == []


def test_a_denied_category_never_reaches_razorpay(client):
    session = open_session(client, [{"sku": "sku_rummy_credits", "quantity": 1}])
    done = client.post(f"/agent/checkout_sessions/{session['session_id']}/complete").json()
    assert (done["outcome"], done["binding_check"]) == ("DENY", "CATEGORY_ALLOWED")
    assert client.stub.orders == []


def test_an_allowed_cart_creates_exactly_one_order_even_if_completed_twice(client):
    session = open_session(client, [{"sku": "sku_atta_5kg", "quantity": 1}])
    url = f"/agent/checkout_sessions/{session['session_id']}/complete"
    first = client.post(url).json()
    second = client.post(url).json()
    assert first["outcome"] == "ALLOW"
    assert second["outcome"] == "ALLOW"
    assert len(client.stub.orders) == 1, "a retry must not create a second order"


def test_step_up_holds_until_a_human_approves(client):
    session = open_session(client, [{"sku": "sku_backup_2tb_annual", "quantity": 1}])
    done = client.post(f"/agent/checkout_sessions/{session['session_id']}/complete").json()
    assert done["outcome"] == "STEP_UP"
    assert client.stub.orders == [], "nothing may be ordered while awaiting approval"

    approved = client.post(
        f"/bound/step_up/{session['session_id']}/approve", json={"approver": "user_daksh"}
    ).json()
    assert approved["outcome"] == "ALLOW"

    done2 = client.post(f"/agent/checkout_sessions/{session['session_id']}/complete").json()
    assert done2["outcome"] == "ALLOW"
    assert len(client.stub.orders) == 1


def test_revoking_stops_the_next_purchase_immediately(client):
    client.post(f"/bound/grants/{client.grant_id}/revoke", json={"reason": "demo"})
    session = open_session(client, [{"sku": "sku_atta_5kg", "quantity": 1}])
    done = client.post(f"/agent/checkout_sessions/{session['session_id']}/complete").json()
    assert (done["outcome"], done["binding_check"]) == ("DENY", "GRANT_REVOKED")


def test_every_decision_lands_in_the_chain_and_it_verifies(client):
    open_session(client, [{"sku": "sku_atta_5kg", "quantity": 1}])
    body = client.get("/explain/chain").json()
    assert body["verification"]["ok"] is True
    assert any(e["type"] == "DECISION_PREVIEW" for e in body["entries"])


def test_healthz_reports_what_is_actually_configured(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["test_mode_enforced"] is True
    assert body["issuer_key_loaded"] is True
    assert body["chain"]["ok"] is True


def test_unknown_grant_and_session_are_404(client):
    assert client.post(
        "/agent/checkout_sessions",
        json={"agent_id": "a", "grant_id": "gnt_nope", "items": [{"sku": "sku_atta_5kg"}]},
    ).status_code == 404
    assert client.get("/agent/checkout_sessions/ses_nope").status_code == 404
