"""Billing endpoints — subscription status, checkout, portal, usage, addon, webhook."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from p8.api.deps import CurrentUser, get_current_user, get_db
from p8.services.database import Database
from p8.services.stripe import StripeService
from p8.services.usage import get_all_usage, get_user_plan

router = APIRouter()
webhook_router = APIRouter()


def _get_stripe(request: Request) -> StripeService:
    svc = request.app.state.stripe_service
    if not svc:
        raise HTTPException(503, "Stripe not configured")
    return svc  # type: ignore[no-any-return]


class CheckoutRequest(BaseModel):
    plan_id: str


class AddonRequest(BaseModel):
    addon_id: str = "chat_tokens_50k"


@router.get("/subscription")
async def get_subscription(
    user: CurrentUser = Depends(get_current_user),
    svc: StripeService = Depends(_get_stripe),
):
    """Current subscription status for the authenticated user."""
    return await svc.get_subscription(user.user_id, user.tenant_id)


@router.get("/usage")
async def get_usage(
    user: CurrentUser = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Current usage across all metered resources."""
    plan_id = await get_user_plan(db, user.user_id, user.tenant_id)
    return await get_all_usage(db, user.user_id, plan_id)


@router.post("/checkout")
async def create_checkout(
    body: CheckoutRequest,
    user: CurrentUser = Depends(get_current_user),
    svc: StripeService = Depends(_get_stripe),
):
    """Create Stripe Checkout Session, return {url} for redirect."""
    try:
        url = await svc.create_checkout_session(
            user.user_id, user.email, user.tenant_id, body.plan_id
        )
        return {"url": url}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/addon")
async def create_addon(
    body: AddonRequest,
    user: CurrentUser = Depends(get_current_user),
    svc: StripeService = Depends(_get_stripe),
):
    """Create a one-time Stripe Checkout for a chat token add-on pack."""
    try:
        url = await svc.create_addon_checkout(
            user.user_id, user.email, user.tenant_id, body.addon_id
        )
        return {"url": url}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/portal")
async def billing_portal(
    user: CurrentUser = Depends(get_current_user),
    svc: StripeService = Depends(_get_stripe),
):
    """Create Stripe Billing Portal session, return {url} for redirect."""
    try:
        url = await svc.create_portal_session(user.user_id, user.tenant_id)
        return {"url": url}
    except ValueError as e:
        raise HTTPException(400, str(e))


@webhook_router.post("/webhooks")
async def stripe_webhook(request: Request):
    """Stripe webhook — signature-verified, no JWT/API key."""
    svc: StripeService = request.app.state.stripe_service
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not sig:
        raise HTTPException(400, "Missing stripe-signature header")
    try:
        return await svc.handle_webhook(payload, sig)
    except Exception as e:
        raise HTTPException(400, f"Webhook error: {e}")
