"""Unit tests for Stripe webhook handlers in StripeService."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from tests.unit.helpers import mock_services


def _make_service():
    """Create a StripeService with mocked DB and settings."""
    db, _, settings, *_ = mock_services()
    settings.stripe_secret_key = "sk_test_fake"
    settings.stripe_webhook_secret = "whsec_fake"
    db.fetchrow = AsyncMock(return_value={"id": uuid4()})

    with patch("p8.services.stripe.stripe"):
        from p8.services.stripe import StripeService
        svc = StripeService(db=db, settings=settings)
    return svc, db


def _stripe_event(event_id, event_type, data_object):
    """Build a fake Stripe event matching the webhook handler's expectations."""
    obj = MagicMock()
    for k, v in data_object.items():
        setattr(obj, k, v)

    event = MagicMock()
    event.id = event_id
    event.type = event_type
    event.data = MagicMock()
    event.data.object = obj
    # dict(event.data) is called for payload storage — return something JSON-serializable
    event.data.__iter__ = MagicMock(return_value=iter([("object", {})]))
    event.data.keys = MagicMock(return_value=["object"])
    event.data.__getitem__ = MagicMock(return_value={})
    return event


@pytest.mark.asyncio
async def test_invoice_payment_failed_marks_past_due():
    svc, db = _make_service()
    event = _stripe_event("evt_fail_1", "invoice.payment_failed", {
        "customer": "cus_123",
    })

    with patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event):
        result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "processed"
    assert result["type"] == "invoice.payment_failed"

    # Should UPDATE with past_due guard
    update_call = db.fetchrow.call_args_list[-1]
    sql = update_call[0][0]
    assert "past_due" in sql
    assert "subscription_status = 'active'" in sql
    assert update_call[0][1] == "cus_123"


@pytest.mark.asyncio
async def test_invoice_payment_failed_idempotent():
    """Duplicate event returns 'duplicate' without processing."""
    svc, db = _make_service()
    event = _stripe_event("evt_dup_1", "invoice.payment_failed", {
        "customer": "cus_123",
    })

    # First call to fetchrow (idempotent insert) returns None → duplicate
    db.fetchrow = AsyncMock(return_value=None)

    with patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event):
        result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "duplicate"


@pytest.mark.asyncio
async def test_charge_refunded_reverses_addon_credits():
    svc, db = _make_service()
    user_id = uuid4()

    event = _stripe_event("evt_refund_1", "charge.refunded", {
        "payment_intent": "pi_addon_123",
    })

    # fetchrow calls: 1) idempotent insert (returns id), 2) payment_intents lookup
    pi_row = {
        "user_id": user_id,
        "metadata": {
            "addon_id": "chat_tokens_50k",
            "resource_type": "chat_tokens",
            "grant_amount": "50000",
        },
    }
    db.fetchrow = AsyncMock(side_effect=[{"id": uuid4()}, pi_row])

    with patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event):
        result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "processed"

    # Should call execute for: usage_tracking reversal + mark processed
    execute_calls = db.execute.call_args_list
    reversal_call = execute_calls[-2]  # second-to-last (last is mark processed)
    sql = reversal_call[0][0]
    assert "GREATEST(granted_extra - $1, 0)" in sql
    assert reversal_call[0][1] == 50000
    assert reversal_call[0][2] == user_id
    assert reversal_call[0][3] == "chat_tokens"


@pytest.mark.asyncio
async def test_charge_refunded_unknown_pi_logs_warning(caplog):
    svc, db = _make_service()

    event = _stripe_event("evt_refund_2", "charge.refunded", {
        "payment_intent": "pi_unknown_999",
    })

    # fetchrow: 1) idempotent insert returns id, 2) payment_intents lookup returns None
    db.fetchrow = AsyncMock(side_effect=[{"id": uuid4()}, None])

    with patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event):
        import logging
        with caplog.at_level(logging.WARNING, logger="p8.services.stripe"):
            result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "processed"
    assert "unknown payment_intent" in caplog.text


@pytest.mark.asyncio
async def test_charge_refunded_subscription_no_action(caplog):
    """Refund on a subscription payment (no addon_id) should log but not reverse credits."""
    svc, db = _make_service()

    event = _stripe_event("evt_refund_3", "charge.refunded", {
        "payment_intent": "pi_sub_456",
    })

    # payment_intents row exists but has no addon_id in metadata
    pi_row = {"user_id": uuid4(), "metadata": {"plan_id": "pro"}}
    db.fetchrow = AsyncMock(side_effect=[{"id": uuid4()}, pi_row])

    with patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event):
        import logging
        with caplog.at_level(logging.INFO, logger="p8.services.stripe"):
            result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "processed"
    # Should NOT have called execute for usage_tracking reversal — only the final "mark processed"
    execute_sqls = [c[0][0] for c in db.execute.call_args_list]
    assert not any("granted_extra" in sql for sql in execute_sqls)
    assert "no credit reversal" in caplog.text


@pytest.mark.asyncio
async def test_checkout_completed_populates_payment_intents():
    svc, db = _make_service()
    user_id = uuid4()

    session_obj = {
        "metadata": {
            "p8_user_id": str(user_id),
            "addon_id": "chat_tokens_50k",
            "resource_type": "chat_tokens",
            "grant_amount": "50000",
        },
        "payment_intent": "pi_checkout_789",
        "customer": "cus_456",
        "amount_total": 200,
        "currency": "usd",
        "subscription": None,
    }
    event = _stripe_event("evt_checkout_1", "checkout.session.completed", session_obj)
    # metadata needs to be a real dict for .get() calls
    event.data.object.metadata = session_obj["metadata"]

    with patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event):
        result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "processed"

    # Find the payment_intents INSERT among execute calls
    pi_calls = [
        c for c in db.execute.call_args_list
        if "payment_intents" in c[0][0]
    ]
    assert len(pi_calls) == 1
    sql = pi_calls[0][0][0]
    assert "INSERT INTO payment_intents" in sql
    assert pi_calls[0][0][1] == user_id  # user_id
    assert pi_calls[0][0][2] == "pi_checkout_789"  # stripe_payment_intent_id


@pytest.mark.asyncio
async def test_unknown_price_defaults_to_free():
    """_update_subscription_from_stripe should default unknown prices to 'free'."""
    svc, db = _make_service()

    sub = _stripe_subscription_obj(
        sub_id="sub_999", customer="cus_789", status="active",
        price_id="price_UNKNOWN_xyz", period_end=1700000000,
    )

    await svc._update_subscription_from_stripe(sub)

    # Should have called execute with plan_id = "free"
    call = db.execute.call_args
    assert call[0][1] == "free"


# ---------------------------------------------------------------------------
# Stripe SDK v14 compatibility tests
#
# In Stripe SDK v14+, subscription.items collides with dict.items(),
# so code must use bracket access (sub["items"]) instead of dot access
# (sub.items). These tests use dict-like objects that mimic real SDK
# behavior to catch regressions.
# ---------------------------------------------------------------------------


class _StripeObj(dict):
    """Mimics Stripe SDK v14 objects: bracket access works, but dot-access
    for 'items' returns dict.items (a method), not the subscriptions list."""
    def __getattr__(self, name):
        if name == "items":
            # This is the actual SDK v14 bug: dict.items() shadows the field
            return super().items
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def get(self, key, default=None):
        return super().get(key, default)


def _stripe_subscription_obj(
    *, sub_id="sub_test", customer="cus_test", status="active",
    price_id="price_1T2vwj39KSGRMEM5YimcE0SK", period_end=1700000000,
):
    """Build a Stripe Subscription-like object matching SDK v14 behavior."""
    return _StripeObj({
        "id": sub_id,
        "customer": customer,
        "status": status,
        "current_period_end": period_end,
        "items": _StripeObj({
            "data": [
                _StripeObj({"price": _StripeObj({"id": price_id})}),
            ],
        }),
    })


@pytest.mark.asyncio
async def test_update_subscription_sdk_v14_bracket_access():
    """_update_subscription_from_stripe must use bracket access, not dot access,
    because Stripe SDK v14 shadows sub.items with dict.items()."""
    svc, db = _make_service()

    sub = _stripe_subscription_obj(
        sub_id="sub_v14", customer="cus_v14", status="active",
        price_id="price_1T2vwj39KSGRMEM5YimcE0SK",  # "pro" price
        period_end=1700000000,
    )

    await svc._update_subscription_from_stripe(sub)

    call = db.execute.call_args
    assert call[0][1] == "pro"          # plan_id
    assert call[0][2] == "active"       # status
    assert call[0][3] == "sub_v14"      # subscription_id
    assert call[0][5] == "cus_v14"      # customer_id


@pytest.mark.asyncio
async def test_update_subscription_sdk_v14_no_period_end():
    """Should handle missing current_period_end gracefully."""
    svc, db = _make_service()

    sub = _stripe_subscription_obj(
        sub_id="sub_noend", customer="cus_noend", status="active",
        period_end=None,
    )

    await svc._update_subscription_from_stripe(sub)

    call = db.execute.call_args
    assert call[0][1] == "pro"
    assert call[0][4] is None  # period_end should be None


@pytest.mark.asyncio
async def test_checkout_completed_subscription_upgrade_sdk_v14():
    """Full webhook flow: checkout.session.completed → subscription upgrade."""
    svc, db = _make_service()

    # The checkout session has a subscription ID
    session_obj = {
        "metadata": {"p8_user_id": str(uuid4()), "plan_id": "pro"},
        "subscription": "sub_checkout_v14",
        "payment_intent": None,
        "customer": "cus_checkout",
        "amount_total": 900,
        "currency": "usd",
    }
    event = _stripe_event("evt_checkout_v14", "checkout.session.completed", session_obj)
    event.data.object.metadata = session_obj["metadata"]
    event.data.object.subscription = "sub_checkout_v14"

    # Mock stripe.Subscription.retrieve to return an SDK v14-like object
    stripe_sub = _stripe_subscription_obj(
        sub_id="sub_checkout_v14", customer="cus_checkout", status="active",
    )

    with (
        patch("p8.services.stripe.stripe.Webhook.construct_event", return_value=event),
        patch("p8.services.stripe.stripe.Subscription.retrieve", return_value=stripe_sub),
    ):
        result = await svc.handle_webhook(b"payload", "sig")

    assert result["status"] == "processed"

    # Verify the plan was updated to "pro"
    update_calls = [
        c for c in db.execute.call_args_list
        if "plan_id" in c[0][0]
    ]
    assert len(update_calls) == 1
    assert update_calls[0][0][1] == "pro"
