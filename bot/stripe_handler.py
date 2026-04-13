import os
import stripe
import logging

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

logger = logging.getLogger(__name__)

STRIPE_PRICES = {
    "small": {
        "price_id": "price_1TLetkLhGc6RmyWQCzBtrlmH",
        "posts": 100,
        "price_eur": 3.99,
        "label": "100 постов",
    },
    "medium": {
        "price_id": "price_1TLetkLhGc6RmyWQLcHxycOa",
        "posts": 300,
        "price_eur": 7.99,
        "label": "300 постов",
    },
    "large": {
        "price_id": "price_1TLetkLhGc6RmyWQ8cvtIsTI",
        "posts": 1500,
        "price_eur": 33.99,
        "label": "1500 постов",
    },
}


async def create_checkout_session(user_id: int, tier: str) -> str:
    """Create Stripe Checkout Session. Returns URL for user redirect."""
    if tier not in STRIPE_PRICES:
        raise ValueError(f"Unknown tier: {tier}")

    price_data = STRIPE_PRICES[tier]
    base_url = os.getenv("BOT_RAILWAY_URL", "https://parser-bot-production.up.railway.app")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{"price": price_data["price_id"], "quantity": 1}],
        client_reference_id=str(user_id),
        # Pass user_id + tier in payment_intent metadata so webhook can read it
        payment_intent_data={
            "metadata": {
                "user_id": str(user_id),
                "tier": tier,
            }
        },
        success_url=f"{base_url}/stripe-success",
        cancel_url=f"{base_url}/stripe-cancel",
    )
    return session.url


async def process_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify Stripe signature and return parsed event data."""
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return {"error": "Invalid payload"}
    except stripe.error.SignatureVerificationError:
        return {"error": "Invalid signature"}

    event_type = event["type"]

    if event_type == "payment_intent.succeeded":
        pi = event["data"]["object"]
        user_id = int(pi.get("metadata", {}).get("user_id", 0))
        tier = pi.get("metadata", {}).get("tier")
        if user_id and tier and tier in STRIPE_PRICES:
            posts = STRIPE_PRICES[tier]["posts"]
            logger.info(f"✅ Payment succeeded: user={user_id}, tier={tier}, posts={posts}")
            return {"status": "ok", "event": "succeeded", "user_id": user_id, "tier": tier, "posts": posts}

    elif event_type == "payment_intent.payment_failed":
        pi = event["data"]["object"]
        user_id = int(pi.get("metadata", {}).get("user_id", 0))
        failure_msg = pi.get("last_payment_error", {}).get("message", "Unknown error")
        logger.warning(f"❌ Payment failed: user={user_id}: {failure_msg}")
        return {"status": "failed", "event": "payment_failed", "user_id": user_id, "error": failure_msg}

    elif event_type == "payment_intent.canceled":
        pi = event["data"]["object"]
        user_id = int(pi.get("metadata", {}).get("user_id", 0))
        logger.info(f"🚫 Payment canceled: user={user_id}")
        return {"status": "cancelled", "event": "payment_canceled", "user_id": user_id}

    elif event_type == "charge.dispute.created":
        charge = event["data"]["object"]
        logger.error(f"⚠️ DISPUTE: {charge.get('id')} reason={charge.get('reason')}")
        return {"status": "alert", "event": "dispute_created", "charge_id": charge.get("id")}

    return {"status": "received"}
