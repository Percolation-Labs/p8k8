"""Stripe billing service — checkout sessions, portal, webhooks.

API Endpoints (p8/api/routers/payments.py)
-------------------------------------------
  GET  /billing/subscription  — Current plan for authenticated user
  GET  /billing/usage         — Metered usage across all resources
  POST /billing/checkout      — Create Stripe Checkout Session (body: {plan_id})
  POST /billing/addon         — One-time add-on purchase (body: {addon_id})
  POST /billing/portal        — Stripe Billing Portal session
  POST /billing/webhooks      — Stripe webhook (signature-verified, no JWT)

Webhook Configuration
----------------------
Endpoint: POST /billing/webhooks
Created via Stripe dashboard (test mode, acct_1T2vF939KSGRMEM5).
Events: checkout.*, customer.subscription.*, invoice.*, charge.*

The signing secret (whsec_...) is stored in three places:
  1. .env — P8_STRIPE_WEBHOOK_SECRET
  2. Shell — export P8_STRIPE_WEBHOOK_SECRET=whsec_...
  3. K8s — p8-app-secrets in namespace p8

Stripe SDK v14 Compatibility
------------------------------
Stripe SDK v14+ makes Subscription objects inherit from dict. This means
sub.items returns dict.items() (a builtin method), NOT the subscription
line items. All access in _update_subscription_from_stripe uses bracket
notation: sub["items"], sub["status"], sub["id"], sub["customer"].

Testing
--------
Unit tests (no DB): uv run pytest tests/unit/test_stripe_webhooks.py
  - invoice.payment_failed marks past_due (with idempotent guard)
  - Duplicate event returns 'duplicate' without processing
  - charge.refunded reverses addon credits via granted_extra
  - charge.refunded on unknown payment_intent logs warning
  - charge.refunded on subscription payment is a no-op
  - checkout.session.completed populates payment_intents audit table
  - Unknown price_id defaults to 'free' plan
  - SDK v14 bracket access (pro upgrade via real dict-like objects)
  - SDK v14 handles missing current_period_end
  - Full checkout → subscription upgrade flow (SDK v14)

Integration tests (needs Postgres):
  uv run pytest tests/integration/billing/  — quotas, usage, API 429s

Test cards (Stripe test mode):
  4242 4242 4242 4242  — Succeeds (any future exp, any CVC/zip)
  4000 0025 0000 3155  — Requires 3D Secure authentication
  4000 0000 0000 9995  — Declined (insufficient funds)
  4000 0000 0000 0341  — Attaching card fails

Local webhook testing:
  stripe listen --forward-to localhost:8000/billing/webhooks
  stripe trigger checkout.session.completed

Live E2E verification (K8s):
  1. Reset user to free: UPDATE stripe_customers SET plan_id='free' WHERE user_id=...
  2. Retrieve real subscription: stripe.Subscription.retrieve(sub_id)
  3. Run handler: await svc._update_subscription_from_stripe(sub)
  4. Verify: SELECT plan_id FROM stripe_customers WHERE user_id=...
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

import stripe

from p8.services.database import Database
from p8.settings import Settings

logger = logging.getLogger(__name__)

# Plan ID → Stripe Price ID mapping
PLAN_PRICES: dict[str, str] = {
    "pro": "price_1T2vwj39KSGRMEM5YimcE0SK",
    "team": "price_1T2vwj39KSGRMEM5ZDPr9DOb",
}

# Reverse lookup: price_id → plan_id
PRICE_TO_PLAN = {v: k for k, v in PLAN_PRICES.items()}

# Add-on packs — one-time purchases
ADDON_PRICES: dict[str, dict] = {
    "chat_tokens_50k": {
        "price_id": "price_1T2vwj39KSGRMEM5AddonTok",
        "resource_type": "chat_tokens",
        "grant_amount": 50_000,
        "display_name": "50K Chat Tokens",
        "amount_cents": 200,  # $2.00
    },
}


class StripeService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        stripe.api_key = settings.stripe_secret_key
        self.webhook_secret = settings.stripe_webhook_secret

    async def _ensure_customer(
        self, user_id: UUID, email: str, tenant_id: str | None
    ) -> str:
        """Get or create Stripe customer, return stripe_customer_id."""
        row = await self.db.fetchrow(
            "SELECT stripe_customer_id FROM stripe_customers "
            "WHERE user_id = $1 AND tenant_id IS NOT DISTINCT FROM $2 AND deleted_at IS NULL",
            user_id, tenant_id,
        )
        if row:
            return row["stripe_customer_id"]  # type: ignore[no-any-return]

        customer = stripe.Customer.create(
            email=email,
            metadata={"p8_user_id": str(user_id), "tenant_id": tenant_id or ""},
        )
        await self.db.execute(
            "INSERT INTO stripe_customers (user_id, tenant_id, stripe_customer_id, email) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, tenant_id) WHERE deleted_at IS NULL DO NOTHING",
            user_id, tenant_id, customer.id, email,
        )
        return customer.id

    async def get_subscription(self, user_id: UUID, tenant_id: str | None) -> dict:
        """Return current subscription status from local DB (no Stripe API call)."""
        row = await self.db.fetchrow(
            "SELECT plan_id, subscription_status, stripe_customer_id, current_period_end "
            "FROM stripe_customers "
            "WHERE user_id = $1 AND tenant_id IS NOT DISTINCT FROM $2 AND deleted_at IS NULL",
            user_id, tenant_id,
        )
        if not row:
            return {"plan_id": "free", "plan_name": "Free", "status": "active"}

        return {
            "plan_id": row["plan_id"],
            "plan_name": row["plan_id"].capitalize(),
            "status": row["subscription_status"],
            "current_period_end": row["current_period_end"].isoformat() if row["current_period_end"] else None,
            "stripe_customer_id": row["stripe_customer_id"],
        }

    async def create_checkout_session(
        self, user_id: UUID, email: str, tenant_id: str | None, plan_id: str
    ) -> str:
        """Create a Stripe Checkout Session and return the URL."""
        customer_id = await self._ensure_customer(user_id, email, tenant_id)
        price_id = PLAN_PRICES.get(plan_id)
        if not price_id:
            raise ValueError(f"Unknown plan: {plan_id}")

        base = self.settings.api_base_url
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="remapp://billing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="remapp://billing/cancel",
            metadata={"p8_user_id": str(user_id), "plan_id": plan_id},
        )
        return session.url  # type: ignore[return-value]

    async def create_addon_checkout(
        self, user_id: UUID, email: str, tenant_id: str | None, addon_id: str
    ) -> str:
        """Create a one-time Stripe Checkout for an add-on pack, return URL."""
        addon = ADDON_PRICES.get(addon_id)
        if not addon:
            raise ValueError(f"Unknown addon: {addon_id}")

        customer_id = await self._ensure_customer(user_id, email, tenant_id)
        base = self.settings.api_base_url
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="payment",
            line_items=[{"price": addon["price_id"], "quantity": 1}],
            success_url="remapp://billing/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="remapp://billing/cancel",
            metadata={
                "p8_user_id": str(user_id),
                "addon_id": addon_id,
                "resource_type": addon["resource_type"],
                "grant_amount": str(addon["grant_amount"]),
            },
        )
        return session.url  # type: ignore[return-value]

    async def create_portal_session(
        self, user_id: UUID, tenant_id: str | None
    ) -> str:
        """Create a Stripe Billing Portal session and return the URL."""
        row = await self.db.fetchrow(
            "SELECT stripe_customer_id FROM stripe_customers "
            "WHERE user_id = $1 AND tenant_id IS NOT DISTINCT FROM $2 AND deleted_at IS NULL",
            user_id, tenant_id,
        )
        if not row:
            raise ValueError("No billing account found")

        session = stripe.billing_portal.Session.create(
            customer=row["stripe_customer_id"],
            return_url=self.settings.api_base_url,
        )
        return session.url  # type: ignore[return-value]

    async def _update_subscription_from_stripe(self, sub) -> None:
        """Update local stripe_customers row from a Stripe Subscription object.

        Uses bracket access throughout because Stripe SDK v14+ shadows
        ``sub.items`` with ``dict.items()``.
        """
        items_data = sub["items"]["data"] if sub.get("items") else []
        price_id: str | None = items_data[0]["price"]["id"] if items_data else None
        plan_id = PRICE_TO_PLAN.get(price_id, "free") if price_id else "free"
        raw_end = sub.get("current_period_end")
        period_end = datetime.fromtimestamp(raw_end, tz=timezone.utc) if raw_end else None

        await self.db.execute(
            "UPDATE stripe_customers SET "
            "  plan_id = $1, subscription_status = $2, "
            "  stripe_subscription_id = $3, current_period_end = $4 "
            "WHERE stripe_customer_id = $5",
            plan_id, sub["status"], sub["id"], period_end, sub["customer"],
        )
        logger.info("Updated customer %s → plan=%s status=%s", sub["customer"], plan_id, sub["status"])

    async def handle_webhook(self, payload: bytes, sig_header: str) -> dict:
        """Verify and process a Stripe webhook event."""
        event = stripe.Webhook.construct_event(payload, sig_header, self.webhook_secret)

        # Idempotent insert
        inserted = await self.db.fetchrow(
            "INSERT INTO webhook_events (stripe_event_id, event_type, payload) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id",
            event.id, event.type, dict(event.data),
        )
        if not inserted:
            return {"status": "duplicate", "event_id": event.id}

        # Subscription lifecycle → update local plan
        if event.type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            sub = event.data.object
            if event.type == "customer.subscription.deleted":
                # Downgrade to free on cancellation
                cust_id = sub["customer"]
                await self.db.execute(
                    "UPDATE stripe_customers SET "
                    "  plan_id = 'free', subscription_status = 'canceled', "
                    "  stripe_subscription_id = NULL, current_period_end = NULL "
                    "WHERE stripe_customer_id = $1",
                    cust_id,
                )
                logger.info("Customer %s downgraded to free (subscription deleted)", cust_id)
            else:
                await self._update_subscription_from_stripe(sub)

        # Checkout completed → subscription upgrade or add-on credit
        elif event.type == "checkout.session.completed":
            session = event.data.object
            metadata = session.metadata or {}

            if metadata.get("addon_id"):
                # One-time add-on purchase — credit granted_extra
                user_id_str = metadata.get("p8_user_id")
                resource_type = metadata.get("resource_type")
                grant_amount = int(metadata.get("grant_amount", 0))
                if user_id_str and resource_type and grant_amount:
                    from uuid import UUID as _UUID
                    await self.db.execute(
                        "INSERT INTO usage_tracking (user_id, resource_type, period_start, granted_extra) "
                        "VALUES ($1, $2, date_trunc('month', CURRENT_DATE)::date, $3) "
                        "ON CONFLICT (user_id, resource_type, period_start) "
                        "DO UPDATE SET granted_extra = usage_tracking.granted_extra + $3",
                        _UUID(user_id_str), resource_type, grant_amount,
                    )
                    logger.info("Credited %s +%d %s (addon %s)", user_id_str, grant_amount, resource_type, metadata["addon_id"])

                # Record payment intent for audit trail (enables refund lookups)
                pi_id = session.payment_intent
                if pi_id and user_id_str:
                    await self.db.execute(
                        "INSERT INTO payment_intents "
                        "(user_id, stripe_payment_intent_id, stripe_customer_id, "
                        " amount, currency, status, description, metadata) "
                        "VALUES ($1, $2, $3, $4, $5, 'succeeded', $6, $7) "
                        "ON CONFLICT (stripe_payment_intent_id) DO NOTHING",
                        _UUID(user_id_str), pi_id, session.customer,
                        session.amount_total or 0, session.currency or "usd",
                        f"Addon: {metadata.get('addon_id', '')}",
                        metadata,
                    )
            elif session.subscription:
                sub = stripe.Subscription.retrieve(session.subscription)
                await self._update_subscription_from_stripe(sub)

        # Invoice payment failed → mark subscription as past_due
        elif event.type == "invoice.payment_failed":
            invoice = event.data.object
            customer_id = invoice.customer
            result = await self.db.fetchrow(
                "UPDATE stripe_customers SET subscription_status = 'past_due' "
                "WHERE stripe_customer_id = $1 AND subscription_status = 'active' "
                "RETURNING id",
                customer_id,
            )
            if result:
                logger.info("Customer %s marked past_due (invoice.payment_failed)", customer_id)
            else:
                logger.info("Customer %s already past_due or not active, skipping", customer_id)

        # Charge refunded → reverse addon credits if applicable
        elif event.type == "charge.refunded":
            charge = event.data.object
            pi_id = charge.payment_intent
            if pi_id:
                pi_row = await self.db.fetchrow(
                    "SELECT user_id, metadata FROM payment_intents "
                    "WHERE stripe_payment_intent_id = $1",
                    pi_id,
                )
                if pi_row and pi_row["metadata"].get("addon_id"):
                    pi_meta = pi_row["metadata"]
                    resource_type = pi_meta.get("resource_type")
                    grant_amount = int(pi_meta.get("grant_amount", 0))
                    if resource_type and grant_amount:
                        await self.db.execute(
                            "UPDATE usage_tracking "
                            "SET granted_extra = GREATEST(granted_extra - $1, 0) "
                            "WHERE user_id = $2 AND resource_type = $3 "
                            "AND period_start = date_trunc('month', CURRENT_DATE)::date",
                            grant_amount, pi_row["user_id"], resource_type,
                        )
                        logger.info(
                            "Reversed %d %s for user %s (refund on %s)",
                            grant_amount, resource_type, pi_row["user_id"], pi_id,
                        )
                elif pi_row:
                    logger.info("Refund on subscription payment %s — no credit reversal needed", pi_id)
                else:
                    logger.warning("Refund on unknown payment_intent %s — no action taken", pi_id)

        await self.db.execute(
            "UPDATE webhook_events SET processed = TRUE WHERE stripe_event_id = $1",
            event.id,
        )
        logger.info("Processed webhook %s (%s)", event.id, event.type)
        return {"status": "processed", "event_id": event.id, "type": event.type}
