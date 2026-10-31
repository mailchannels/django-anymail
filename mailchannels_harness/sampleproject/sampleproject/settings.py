"""Minimal Django project that sends with Anymail's MailChannels backend.

Everything is synthetic: the API key is fake and the API URL points at the
local OpenAPI-validating stub (stub_api.py).
"""

import os

SECRET_KEY = "synthetic-not-secret"
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
INSTALLED_APPS = ["anymail", "sampleapp"]
ROOT_URLCONF = "sampleproject.urls"
DATABASES = {}
USE_TZ = True

# The only email configuration the application needs:
EMAIL_BACKEND = "anymail.backends.mailchannels.EmailBackend"
ANYMAIL = {
    "MAILCHANNELS_API_KEY": "synthetic-api-key",
    "MAILCHANNELS_API_URL": os.environ["STUB_API_URL"],
    "MAILCHANNELS_WEBHOOK_PUBLIC_KEY": os.environ["WEBHOOK_PUBLIC_KEY"],
}
DEFAULT_FROM_EMAIL = "app@example.com"

# Where sampleapp records the tracking signals it receives
TRACKING_LOG = os.environ["TRACKING_LOG"]
