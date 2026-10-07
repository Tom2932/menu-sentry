"""
Read-only Stripe check: has each client's subscription been paid?

It only sends GET requests, so a restricted key with Read access to Customers and
Subscriptions is enough. Nothing here can charge, refund or change anything in Stripe.

Clients are matched to Stripe customers by email address: the client's billing_email
if set, otherwise the first address in their report emails. It must be the email the
client used when they paid.
"""

import requests

API = "https://api.stripe.com/v1"


class StripeError(Exception):
    """A problem with the key or permissions that affects every client."""


def _get(key, path, params):
    r = requests.get(f"{API}{path}", params=params, auth=(key, ""), timeout=30)
    if r.status_code == 401:
        raise StripeError("Stripe rejected the key. Check it was copied in full.")
    if r.status_code == 403:
        raise StripeError("The key does not have permission. It needs Read access to "
                          "Customers and Subscriptions.")
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise requests.RequestException(f"Stripe error {r.status_code}: {msg}")
    return r.json()


def summarise(statuses):
    """Turn a list of Stripe subscription statuses into one status for the client."""
    s = set(statuses)
    if s & {"active", "trialing"}:
        return "paid"
    if "past_due" in s:
        return "past_due"
    if "unpaid" in s:
        return "unpaid"
    if "incomplete" in s:
        return "incomplete"
    if s & {"canceled", "incomplete_expired", "paused"}:
        return "cancelled"
    return "no_subscription"


def status_for_email(key, email):
    """Return (status, detail) for the customer(s) with this email."""
    customers = _get(key, "/customers", {"email": email, "limit": 10})["data"]
    if not customers and email != email.lower():
        customers = _get(key, "/customers", {"email": email.lower(), "limit": 10})["data"]
    if not customers:
        return "no_customer", f"No Stripe customer with the email {email}"
    statuses = []
    for cust in customers:
        subs = _get(key, "/subscriptions", {"customer": cust["id"], "status": "all", "limit": 10})["data"]
        statuses += [s["status"] for s in subs]
    return summarise(statuses), ", ".join(sorted(set(statuses))) or "no subscriptions"


def check_all(key, clients):
    """Return {business: (status, detail)} for every client that is not a trial."""
    out = {}
    for cl in clients:
        if cl.get("trial"):
            continue
        email = cl.get("billing_email") or cl["emails"][0]
        try:
            out[cl["business"]] = status_for_email(key, email)
        except requests.RequestException as e:
            out[cl["business"]] = ("error", str(e))
    return out


def ping(key):
    """Confirm the key works and can read both customers and subscriptions."""
    _get(key, "/customers", {"limit": 1})
    _get(key, "/subscriptions", {"limit": 1, "status": "all"})
    return True


# ---------------------------------------------------------------------------
# Stripe Checkout (hosted page): create a payment link for one client.
# Needs a key with Write access to Checkout Sessions as well as the Read access above.
# ---------------------------------------------------------------------------

# sample_only values. Replace them on the dashboard Settings page.
# Every one is listed in STRIPE_INTEGRATION_TODO.md.
PLACEHOLDER_PRICE = "price_..."
PLACEHOLDER_SUCCESS_URL = "https://example.com/success?session_id={CHECKOUT_SESSION_ID}"
PLACEHOLDER_CANCEL_URL = "https://example.com/cancel"
CHECKOUT_MODE = "subscription"   # monthly fee. Use "payment" for a one-off charge.

# fixed_by_ui values, as configured in Checkout Studio.
CHECKOUT_FIXED = {
    "billing_address_collection": "auto",
    "payment_method_collection": "always",       # sent in subscription mode only
    "allow_promotion_codes": False,
    # Shows a tick box for your terms. Stripe needs your Terms of service URL set in the Dashboard
    # (Settings, Business, Public details) or it will not create any payment page.
    "consent_collection": {"terms_of_service": "required"},
    "submit_type": "auto",                       # sent in payment mode only
    "integration_identifier": "hosted_web_0004",
    "saved_payment_method_options": {"payment_method_save": "enabled"},
    "origin_context": "web",
}


# Settings from Checkout Studio that some Stripe accounts may not accept yet. If Stripe says it does not
# recognise one, it is dropped and the request is retried, so a new setting cannot block a customer paying.
OPTIONAL_PARAMS = ("integration_identifier", "origin_context")


def is_placeholder(value):
    return (not value) or "..." in value or "example.com" in value


def _flatten(prefix, value, out):
    """Turn nested values into Stripe's form format, e.g. line_items[0][price]."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]", v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out[prefix] = "true" if value else "false"
    elif value is not None:
        out[prefix] = str(value)


def checkout_params(price_id, success_url, cancel_url, customer_email=None,
                    client_reference_id=None, ui_mode="hosted_page", skip=()):
    fixed = dict(CHECKOUT_FIXED)
    if CHECKOUT_MODE != "subscription":
        fixed.pop("payment_method_collection", None)   # Stripe allows it in subscription mode only
    if CHECKOUT_MODE != "payment":
        fixed.pop("submit_type", None)           # Stripe allows it in payment mode only
    params = {
        "ui_mode": ui_mode,
        "mode": CHECKOUT_MODE,
        **fixed,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [{"price": price_id, "quantity": 1}],
    }
    if customer_email:
        params["customer_email"] = customer_email
    if client_reference_id:
        params["client_reference_id"] = client_reference_id
    flat = {}
    for k, v in params.items():
        _flatten(k, v, flat)
    for k in skip:
        flat.pop(k, None)
    return flat


def create_checkout_session(key, price_id, success_url, cancel_url, customer_email=None,
                            client_reference_id=None):
    """Create a hosted Checkout Session and return Stripe's response (it includes 'url')."""
    # No API version is sent, so Stripe uses the account's default. Newer accounts call the hosted
    # page "hosted_page" and older ones "hosted", so try the new name first and fall back once.
    skip = set()
    for ui_mode in ("hosted_page", "hosted"):
        while True:
            r = requests.post(
                f"{API}/checkout/sessions",
                data=checkout_params(price_id, success_url, cancel_url, customer_email,
                                     client_reference_id, ui_mode, skip),
                auth=(key, ""), timeout=30)
            if r.status_code < 400:
                return r.json()
            if r.status_code == 401:
                raise StripeError("Stripe rejected the key. Check it was copied in full.")
            if r.status_code == 403:
                raise StripeError("The key cannot create payment pages. In Stripe, edit the restricted key "
                                  "and give it Write access to Checkout Sessions.")
            try:
                err = r.json().get("error", {})
            except Exception:
                err = {}
            param = err.get("param")
            if err.get("code") == "parameter_unknown" and param in OPTIONAL_PARAMS and param not in skip:
                skip.add(param)          # this account does not know that setting: drop it and retry
                continue
            if param == "ui_mode" and ui_mode == "hosted_page":
                break                    # try the older name
            message = err.get("message") or f"Stripe error {r.status_code}"
            if "terms of service" in message.lower() or "consent_collection" in (param or ""):
                raise StripeError("Stripe needs your Terms of service URL before it can show the terms tick box. "
                                  "In Stripe, open Settings, then Business, then Public details, and enter the "
                                  "address of your terms page, for example https://www.yourdomain.co.uk/terms. "
                                  "Then try again.")
            raise StripeError(message)
    raise StripeError("Stripe did not accept the payment page settings.")
