MENU SENTRY WEBSITE: FILES FOR THE PUBLIC HOST

This folder holds ONLY what belongs on the internet:
  website.py, payments.py, templates/, public/, requirements.txt, .gitignore

It does NOT contain the dashboard, your clients, your email password or config.json. Never add them.

Settings for the host (Render shown):
  Build command:  pip install -r requirements.txt
  Start command:  gunicorn website:app

Environment variables to add on the host:
  STRIPE_API_KEY     the website key (Checkout Sessions Write only)
  STRIPE_PRICE_ID    price_...
  DOMAIN             https://www.yourdomain.co.uk   (the exact address customers use)
  ADMIN_PASSWORD     a long password for /admin
  SECRET_KEY         a long random string
  CONTACT_EMAIL      your support email
  LEGAL_NAME         your name or company name
  BUSINESS_ADDRESS   your address
  REVIEWS_DB         /var/data/reviews.db   (needs a persistent disk mounted at /var/data)

Without a persistent disk, reviews are deleted every time the site restarts or redeploys.
