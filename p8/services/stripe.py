"""Stripe billing service — checkout sessions, portal, webhooks."""

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
            return row["stripe_customer_id"]

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
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing/cancel",
            metadata={"p8_user_id": str(user_id), "plan_id": plan_id},
        )
        return session.url

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
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing/cancel",
            metadata={
                "p8_user_id": str(user_id),
                "addon_id": addon_id,
                "resource_type": addon["resource_type"],
                "grant_amount": str(addon["grant_amount"]),
            },
        )
        return session.url

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
        return session.url

    async def _update_subscription_from_stripe(self, sub) -> None:
        """Update local stripe_customers row from a Stripe Subscription object."""
        price_id = sub.items.data[0].price.id if sub.items.data else None
        plan_id = PRICE_TO_PLAN.get(price_id, "pro")
        period_end = datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc) if sub.current_period_end else None

        await self.db.execute(
            "UPDATE stripe_customers SET "
            "  plan_id = $1, subscription_status = $2, "
            "  stripe_subscription_id = $3, current_period_end = $4 "
            "WHERE stripe_customer_id = $5",
            plan_id, sub.status, sub.id, period_end, sub.customer,
        )
        logger.info("Updated customer %s → plan=%s status=%s", sub.customer, plan_id, sub.status)

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
                await self.db.execute(
                    "UPDATE stripe_customers SET "
                    "  plan_id = 'free', subscription_status = 'canceled', "
                    "  stripe_subscription_id = NULL, current_period_end = NULL "
                    "WHERE stripe_customer_id = $1",
                    sub.customer,
                )
                logger.info("Customer %s downgraded to free (subscription deleted)", sub.customer)
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
            elif session.subscription:
                sub = stripe.Subscription.retrieve(session.subscription)
                await self._update_subscription_from_stripe(sub)

        await self.db.execute(
            "UPDATE webhook_events SET processed = TRUE WHERE stripe_event_id = $1",
            event.id,
        )
        logger.info("Processed webhook %s (%s)", event.id, event.type)
        return {"status": "processed", "event_id": event.id, "type": event.type}
