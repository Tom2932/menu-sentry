#!/usr/bin/env python3
"""
Menu Sentry public website: home page, sign-up (Stripe Checkout), reviews, terms and privacy.

This is separate from dashboard.py on purpose. Never put the dashboard on the internet.
Only website.py, payments.py, the templates folder and the public folder go on a public server.

Try it on your own computer:   py website.py      (then open http://127.0.0.1:5056)

Settings come from environment variables:
  STRIPE_API_KEY    restricted key with ONLY Write access to Checkout Sessions
  STRIPE_PRICE_ID   your monthly price, starts with price_
  DOMAIN            public address of the site, for example https://www.yourdomain.com
  ADMIN_PASSWORD    password for /admin (reviews). Without it the admin area is switched off.
  SECRET_KEY        a long random string, so admin logins survive restarts
  PRODUCT_NAME      default "Menu Sentry"
  PRICE_TEXT        default "£40 per month". Must match the Stripe price.
  PRICE_NOTE        optional, for example "plus VAT"
  CONTACT_EMAIL     shown on the site and used for support
  LEGAL_NAME        your name or company name, shown in the footer and terms
  BUSINESS_ADDRESS  optional, shown in the footer
  ICO_REGISTRATION  optional, your ICO registration number if you pay the data protection fee
  REVIEWS_DB        where reviews are stored, default reviews.db next to this file
For local testing only, STRIPE_API_KEY and STRIPE_PRICE_ID fall back to the Stripe settings saved by
the dashboard. Do not copy config.json to a public server. It holds client data and passwords.
"""

import hmac
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, redirect, render_template, request,
                   send_from_directory, session)

import payments

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CW_CONFIG", BASE / "config.json"))


def _pick_db_path():
    """Store reviews next to this file, or in your home folder if that folder is read-only."""
    chosen = os.environ.get("REVIEWS_DB")
    if chosen:
        return Path(chosen)
    here = BASE / "reviews.db"
    try:
        probe = sqlite3.connect(here)
        probe.execute("CREATE TABLE IF NOT EXISTS _probe (x)")
        probe.execute("DROP TABLE _probe")
        probe.commit()
        probe.close()
        return here
    except sqlite3.Error:
        fallback = Path.home() / "MenuSentry" / "reviews.db"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cannot write to {BASE}. Reviews will be stored in {fallback} instead.")
        return fallback


REVIEWS_DB = _pick_db_path()
PORT = int(os.environ.get("PORT", 5056))
LEGAL_UPDATED = "7 October 2026"
PUBLIC_FILES = {"style.css", "logo.svg", "logo.png", "logo-transparent.png", "icon.svg", "icon.png"}
REQUIRED_FILES = [
    "templates/base.html", "templates/macros.html", "templates/index.html", "templates/message.html",
    "templates/success.html", "templates/review_form.html", "templates/terms.html", "templates/privacy.html",
    "templates/reviews_policy.html", "templates/admin.html",
    "public/style.css", "public/logo.svg", "public/icon.png",
]
REJECT_REASONS = {
    "spam": "Spam",
    "abusive": "Abusive or offensive",
    "not_real_use": "Not based on real use of the service",
    "withdrawn": "Reviewer withdrew their agreement to publication",
    "other": "Other (explain in the note)",
}

app = Flask(__name__, static_folder=None, template_folder=str(BASE / "templates"))
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("DOMAIN", "").startswith("https://"),
)


# ---------------------------------------------------------------- settings

def setting(env_name, config_key):
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:  # local testing convenience only
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("stripe", {}).get(config_key, "")
    except Exception:
        value = ""
    return "" if payments.is_placeholder(value) else value


def domain():
    return (os.environ.get("DOMAIN") or request.host_url).rstrip("/")


if not os.environ.get("LEGAL_NAME", "").strip():
    app.logger.warning("LEGAL_NAME is not set. The terms say only that %s is a trading name. Set LEGAL_NAME to "
                       "the legal name of the person or company behind the business before you go live.",
                       os.environ.get("PRODUCT_NAME", "Menu Sentry"))


@app.context_processor
def inject_site():
    return {
        "site": {
            "name": os.environ.get("PRODUCT_NAME", "Menu Sentry"),
            "price": os.environ.get("PRICE_TEXT", "\u00a340 per month"),
            "price_note": os.environ.get("PRICE_NOTE", "").strip(),
            "contact": os.environ.get("CONTACT_EMAIL", "").strip(),
            "legal_name": os.environ.get("LEGAL_NAME", "").strip(),
            "address": os.environ.get("BUSINESS_ADDRESS", "").strip(),
            "ico": os.environ.get("ICO_REGISTRATION", "").strip(),
            "year": datetime.now().year,
            "updated": LEGAL_UPDATED,
        },
        "csrf_token": csrf_token,
    }


@app.errorhandler(500)
def server_error(error):
    """Log the real error. Show its details only when you are running the site on your own computer."""
    original = getattr(error, "original_exception", None) or error
    app.logger.error("Server error on %s: %r", request.path, original, exc_info=original)
    detail = ""
    if request.host.startswith(("127.0.0.1", "localhost")):
        detail = f"<p><b>Details (shown only on your own computer):</b><br><code>{type(original).__name__}: " \
                 f"{str(original).replace('<', '&lt;')}</code></p>"
    page = ("<!DOCTYPE html><meta charset='utf-8'><title>Something went wrong</title>"
            "<body style='font-family:system-ui;max-width:640px;margin:60px auto;padding:0 20px'>"
            "<h1>Something went wrong</h1><p>The page could not be shown. The black window running the site "
            "has the full error message.</p>" + detail + "</body>")
    return Response(page, 500, mimetype="text/html")


@app.after_request
def safe_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


# ---------------------------------------------------------------- reviews data

def db():
    con = sqlite3.connect(REVIEWS_DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS invites (token TEXT PRIMARY KEY, business TEXT NOT NULL, "
                "created_at TEXT, used_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, token TEXT UNIQUE, business TEXT, "
                "reviewer TEXT, rating INTEGER, body TEXT, created_at TEXT, status TEXT DEFAULT 'pending', "
                "decided_at TEXT, reject_reason TEXT)")
    return con


def now():
    return datetime.now().isoformat(timespec="seconds")


def review_summary(con):
    """Plain average of every published review. Never adjusted."""
    row = con.execute("SELECT COUNT(*) AS c, AVG(rating) AS a FROM reviews WHERE status='approved'").fetchone()
    count, avg = row["c"], row["a"]
    return {"count": count, "average": f"{avg:.1f}" if count else "", "rounded": int(avg + 0.5) if count else 0}


# ---------------------------------------------------------------- admin protection

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def check_csrf():
    sent, good = request.form.get("csrf", ""), session.get("csrf", "")
    if not good or not hmac.compare_digest(sent, good):
        abort(400)


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not password:
            abort(404)  # admin area is off until a password is set
        auth = request.authorization
        ok = bool(auth) and hmac.compare_digest(auth.username or "", "admin") and \
            hmac.compare_digest(auth.password or "", password)
        if not ok:
            time.sleep(1)  # slow down password guessing
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------- pages

def message(title, text, code=200):
    return render_template("message.html", title=title, text=text), code


@app.route("/")
def index():
    con = db()
    reviews = con.execute("SELECT reviewer, business, rating, body, created_at FROM reviews "
                          "WHERE status='approved' ORDER BY id DESC LIMIT 12").fetchall()
    summary = review_summary(con)
    con.close()
    return render_template("index.html", reviews=reviews, summary=summary)


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/reviews-policy")
def reviews_policy():
    return render_template("reviews_policy.html")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\nDisallow: /review\n", mimetype="text/plain")


@app.route("/<name>")
def public_file(name):
    """Only the files listed in PUBLIC_FILES are served, so nothing else in the folder is exposed."""
    if name not in PUBLIC_FILES:
        abort(404)
    return send_from_directory(BASE / "public", name)


# ---------------------------------------------------------------- checkout

@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    key = setting("STRIPE_API_KEY", "api_key")
    price = setting("STRIPE_PRICE_ID", "price_id")
    if not key or not price:
        app.logger.error("Website is missing STRIPE_API_KEY or STRIPE_PRICE_ID")
        return message("Sign-up is not open yet", "Please check back soon.", 503)
    try:
        session_data = payments.create_checkout_session(
            key, price,
            success_url=f"{domain()}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{domain()}/")   # back to the home page
    except Exception as e:  # never show Stripe error details to a visitor
        app.logger.error("Could not create checkout session: %s", e)
        return message("Sorry, checkout did not start", "Please try again in a minute.", 502)
    return redirect(session_data["url"], code=303)


@app.route("/success")
def success():
    return render_template("success.html")


@app.route("/cancel")
def cancel():
    """Customers who back out of Stripe's page go straight to the home page."""
    return redirect("/", code=303)


# ---------------------------------------------------------------- customer reviews

@app.route("/review", methods=["GET", "POST"])
def review():
    token = (request.form if request.method == "POST" else request.args).get("token", "")
    con = db()
    invite = con.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone() if token else None
    if not invite or invite["used_at"]:
        con.close()
        return message("This review link is not valid",
                       "It may already have been used. If you are a customer and would like to leave a review, "
                       "ask us for a new link.", 404)

    if request.method == "GET":
        con.close()
        return render_template("review_form.html", token=token, business=invite["business"], values={}, errors=[])

    f = request.form
    values = {"reviewer": f.get("reviewer", "").strip(), "body": f.get("body", "").strip(),
              "rating": f.get("rating", "")}
    errors = []
    if not 1 <= len(values["reviewer"]) <= 80:
        errors.append("Enter your name and role (up to 80 characters).")
    if values["rating"] not in {"1", "2", "3", "4", "5"}:
        errors.append("Choose a rating from 1 to 5.")
    if not 20 <= len(values["body"]) <= 1000:
        errors.append("Your review needs to be between 20 and 1000 characters.")
    if not f.get("consent"):
        errors.append("Please tick the box to agree to publication.")
    if f.get("website"):  # hidden field only bots fill in
        con.close()
        return redirect("/review/thanks", code=303)
    if errors:
        con.close()
        return render_template("review_form.html", token=token, business=invite["business"],
                               values=values, errors=errors), 400

    used = con.execute("UPDATE invites SET used_at=? WHERE token=? AND used_at IS NULL", (now(), token))
    if used.rowcount != 1:
        con.close()
        return message("This review link is not valid", "It has already been used.", 409)
    con.execute("INSERT INTO reviews (token, business, reviewer, rating, body, created_at) VALUES (?,?,?,?,?,?)",
                (token, invite["business"], values["reviewer"], int(values["rating"]), values["body"], now()))
    con.commit()
    con.close()
    return redirect("/review/thanks", code=303)


@app.route("/review/thanks")
def review_thanks():
    return message("Thank you for your review",
                   "We read every review before it appears on the site. We publish all genuine reviews, "
                   "whether positive or negative.")


# ---------------------------------------------------------------- admin

@app.route("/admin")
@admin_required
def admin():
    con = db()
    data = {
        "pending": con.execute("SELECT * FROM reviews WHERE status='pending' ORDER BY id").fetchall(),
        "approved": con.execute("SELECT * FROM reviews WHERE status='approved' ORDER BY id DESC").fetchall(),
        "rejected": con.execute("SELECT * FROM reviews WHERE status='rejected' ORDER BY id DESC LIMIT 30").fetchall(),
        "invites": con.execute("SELECT * FROM invites ORDER BY created_at DESC LIMIT 100").fetchall(),
        "summary": review_summary(con),
    }
    con.close()
    return render_template("admin.html", base_url=domain(), reasons=REJECT_REASONS, **data)


@app.route("/admin/invite", methods=["POST"])
@admin_required
def admin_invite():
    check_csrf()
    business = request.form.get("business", "").strip()[:100]
    if business:
        con = db()
        con.execute("INSERT INTO invites (token, business, created_at) VALUES (?,?,?)",
                    (secrets.token_urlsafe(16), business, now()))
        con.commit()
        con.close()
    return redirect("/admin")


@app.route("/admin/invite/<token>/delete", methods=["POST"])
@admin_required
def admin_invite_delete(token):
    check_csrf()
    con = db()
    con.execute("DELETE FROM invites WHERE token=? AND used_at IS NULL", (token,))
    con.commit()
    con.close()
    return redirect("/admin")


@app.route("/admin/review/<int:review_id>/approve", methods=["POST"])
@admin_required
def admin_approve(review_id):
    check_csrf()
    con = db()
    con.execute("UPDATE reviews SET status='approved', decided_at=?, reject_reason=NULL WHERE id=?",
                (now(), review_id))
    con.commit()
    con.close()
    return redirect("/admin")


@app.route("/admin/review/<int:review_id>/reject", methods=["POST"])
@admin_required
def admin_reject(review_id):
    check_csrf()
    reason = request.form.get("reason", "")
    if reason not in REJECT_REASONS:
        abort(400)
    note = request.form.get("note", "").strip()[:300]
    if reason == "other" and not note:
        abort(400)
    text = REJECT_REASONS[reason] + (f": {note}" if note else "")
    con = db()
    con.execute("UPDATE reviews SET status='rejected', decided_at=?, reject_reason=? WHERE id=?",
                (now(), text, review_id))
    con.commit()
    con.close()
    return redirect("/admin")


def port_in_use(host, port):
    """True if something is already listening there, for example an older copy still running."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def missing_files():
    return [f for f in REQUIRED_FILES if not (BASE / f).exists()]


if __name__ == "__main__":
    missing = missing_files()
    if missing:
        print("\nSome website files are missing:")
        for f in missing:
            print("   ", f)
        print("\nKeep website.py, payments.py and the 'templates' and 'public' folders together, exactly as in")
        print("menu-sentry.zip. Right-click the zip, choose Extract All, then run start_website.bat from the")
        print("extracted folder. Do not run it from inside the zip.\n")
        raise SystemExit(1)
    if port_in_use("127.0.0.1", PORT):
        print(f"\nPort {PORT} is already in use, so another copy of this website is probably still running.")
        print("That older copy would keep answering your browser while this window shows nothing.")
        print("Close every other black window, or end python.exe in Task Manager, then start it again.\n")
        raise SystemExit(1)
    host = os.environ.get("HOST", "127.0.0.1")
    if host == "127.0.0.1":  # running on your own computer: open the site in your browser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    print(f"Website running at http://127.0.0.1:{PORT}  (close this window to stop it)")
    app.run(host=host, port=PORT, debug=False)
