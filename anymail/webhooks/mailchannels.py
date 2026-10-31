import binascii
import hashlib
import json
import re
from base64 import b64decode
from datetime import datetime, timezone
from time import time

from django.utils.crypto import constant_time_compare

from ..exceptions import (
    AnymailConfigurationError,
    AnymailImproperlyInstalled,
    AnymailWebhookValidationFailure,
    _LazyError,
)
from ..signals import AnymailTrackingEvent, EventType, RejectReason, tracking
from ..utils import get_anymail_setting
from .base import AnymailBaseWebhookView

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    # This module gets imported by anymail.urls, so don't complain about cryptography
    # missing unless the MailChannels webhook view is actually used and needs it
    error = _LazyError(
        AnymailImproperlyInstalled(
            missing_package="cryptography", install_extra="mailchannels"
        )
    )
    serialization = error
    Ed25519PublicKey = error
    InvalidSignature = Exception


# MailChannels signs webhooks with HTTP Message Signatures (RFC 9421), using a single
# Ed25519 signature that covers the Content-Digest (RFC 9530) of the body. E.g.:
#   Content-Digest: sha-256=:<base64>:
#   Signature-Input: sig_1738775282=("content-digest");created=...;alg="ed25519";keyid="mckey"
#   Signature: sig_1738775282=:<base64>:
# https://docs.mailchannels.com/email-api/webhooks#verifying-message-signatures
SIGNATURE_INPUT_RE = re.compile(
    r"^(?P<label>[a-zA-Z0-9_.*-]+)=(?P<params>\((?P<components>[^()]*)\).*)$"
)
SIGNATURE_PARAM_RE = re.compile(r';(?P<key>[a-z0-9_.*-]+)=(?P<value>"[^"]*"|[^;]*)')
SIGNATURE_RE = re.compile(r"(?P<label>[a-zA-Z0-9_.*-]+)=:(?P<signature>[^:]*):")
CONTENT_DIGEST_RE = re.compile(r"(?:^|,)\s*sha-256=:(?P<digest>[^:]*):")


class MailChannelsTrackingWebhookView(AnymailBaseWebhookView):
    """Handler for MailChannels Email API delivery and engagement webhooks"""

    esp_name = "MailChannels"
    signal = tracking

    # Signed webhooks don't need basic auth (but it's still checked if configured)
    warn_if_no_basic_auth = False

    # These can be set from kwargs in View.as_view, or pulled from settings in init:
    webhook_public_key = None  # base64 raw Ed25519 key, or PEM SubjectPublicKeyInfo

    # Reject signatures created longer ago (or further in the future) than this,
    # to limit replay of captured webhook calls. Override in View.as_view if needed.
    signature_max_age = 300  # seconds
    signature_max_skew = 30  # seconds

    def __init__(self, **kwargs):
        webhook_public_key = get_anymail_setting(
            "webhook_public_key",
            esp_name=self.esp_name,
            kwargs=kwargs,
            allow_bare=True,
        )
        self.verification_key = self.load_public_key(webhook_public_key)
        super().__init__(**kwargs)

    @staticmethod
    def load_public_key(key):
        if isinstance(key, str):
            key = key.strip().encode("ascii")
        if key.startswith(b"-----BEGIN"):
            public_key = serialization.load_pem_public_key(key)
            if not isinstance(public_key, Ed25519PublicKey):
                raise AnymailConfigurationError(
                    "MAILCHANNELS_WEBHOOK_PUBLIC_KEY is not an Ed25519 public key"
                )
            return public_key
        try:
            raw_key = b64decode(key, validate=True)
        except binascii.Error:
            raw_key = b""
        if len(raw_key) != 32:
            raise AnymailConfigurationError(
                "MAILCHANNELS_WEBHOOK_PUBLIC_KEY must be the base64 Ed25519 key"
                " from MailChannels' webhook public-key API (or a PEM public key)"
            )
        return Ed25519PublicKey.from_public_bytes(raw_key)

    def validate_request(self, request):
        signature_input = request.headers.get("Signature-Input", "").strip()
        match = SIGNATURE_INPUT_RE.match(signature_input)
        if not match:
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook has missing or unsupported Signature-Input header"
            )
        label = match["label"]
        signature_params = match["params"]
        params = {
            param["key"]: param["value"].strip('"')
            for param in SIGNATURE_PARAM_RE.finditer(signature_params)
        }
        components = match["components"].split()
        if any(
            not (component.startswith('"') and component.endswith('"'))
            for component in components
        ):
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook has malformed Signature-Input components"
            )
        components = [component.strip('"').lower() for component in components]

        # The signature must cover the body (via its digest), or anything could
        # be posted with a replayed signature.
        if "content-digest" not in components:
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook signature doesn't cover Content-Digest"
            )
        if params.get("alg", "ed25519") != "ed25519":
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook signature uses an unsupported algorithm"
            )
        try:
            created = int(params["created"])
        except (KeyError, ValueError):
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook signature is missing its created time"
            )
        age = time() - created
        if age > self.signature_max_age or age < -self.signature_max_skew:
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook signature has expired (or clocks are out of sync)"
            )

        digest_match = CONTENT_DIGEST_RE.search(
            request.headers.get("Content-Digest", "")
        )
        try:
            expected_digest = b64decode(digest_match["digest"], validate=True)
        except (TypeError, binascii.Error):
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook has missing or invalid Content-Digest header"
            )
        if not constant_time_compare(
            expected_digest, hashlib.sha256(request.body).digest()
        ):
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook body doesn't match its Content-Digest"
            )

        signature = None
        for signature_match in SIGNATURE_RE.finditer(
            request.headers.get("Signature", "")
        ):
            if signature_match["label"] == label:
                signature = signature_match["signature"]
                break
        try:
            signature = b64decode(signature, validate=True)
        except (TypeError, binascii.Error):
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook has missing or invalid Signature header"
            )

        # RFC 9421 section 2.5 signature base
        lines = []
        for component in components:
            if component.startswith("@") or component not in request.headers:
                raise AnymailWebhookValidationFailure(
                    f"MailChannels webhook signature covers unavailable {component!r}"
                )
            lines.append(f'"{component}": {request.headers[component].strip()}')
        lines.append(f'"@signature-params": {signature_params}')
        signature_base = "\n".join(lines).encode("utf-8")

        try:
            self.verification_key.verify(signature, signature_base)
        except InvalidSignature:
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook called with incorrect signature"
            )

    def parse_events(self, request):
        try:
            esp_events = json.loads(request.body.decode("utf-8"))
        except ValueError:
            # The signature was valid, so this isn't expected
            raise AnymailWebhookValidationFailure(
                "MailChannels webhook body is not valid JSON"
            )
        if isinstance(esp_events, dict):
            esp_events = [esp_events]
        return [
            event
            for esp_event in esp_events
            for event in self.esp_to_anymail_events(esp_event)
        ]

    event_types = {
        # Map MailChannels event type: Anymail normalized (event type, reject reason)
        "processed": (EventType.QUEUED, None),
        "delivered": (EventType.DELIVERED, None),
        "dropped": (EventType.REJECTED, RejectReason.OTHER),
        "hard-bounced": (EventType.BOUNCED, RejectReason.BOUNCED),
        "soft-bounced": (EventType.DEFERRED, None),
        "open": (EventType.OPENED, None),
        "click": (EventType.CLICKED, None),
        "complained": (EventType.COMPLAINED, RejectReason.SPAM),
        "unsubscribed": (EventType.UNSUBSCRIBED, RejectReason.UNSUBSCRIBED),
        # "test" (sent when validating a webhook) is EventType.UNKNOWN
    }

    def esp_to_anymail_events(self, esp_event):
        event_type, reject_reason = self.event_types.get(
            esp_event.get("event"), (EventType.UNKNOWN, None)
        )
        try:
            timestamp = datetime.fromtimestamp(
                int(esp_event["timestamp"]), tz=timezone.utc
            )
        except (KeyError, TypeError, ValueError):
            timestamp = None
        status = esp_event.get("status")
        reason = esp_event.get("reason")
        campaign_id = esp_event.get("campaign_id")
        common = dict(
            event_type=event_type,
            timestamp=timestamp,
            # The send API's request_id (also Anymail's message_id at send time).
            # It is blank for messages sent with SMTP.
            message_id=esp_event.get("request_id") or None,
            event_id=None,  # MailChannels doesn't provide a unique event id
            reject_reason=reject_reason,
            description=reason,
            mta_response=" ".join(str(part) for part in (status, reason) if part)
            or None,
            tags=[campaign_id] if campaign_id else [],
            click_url=esp_event.get("url"),
            user_agent=esp_event.get("user_agent"),
            esp_event=esp_event,
        )
        # One MailChannels event can cover several recipients of the same message
        recipients = esp_event.get("recipients") or [None]
        return [
            AnymailTrackingEvent(recipient=recipient, **common)
            for recipient in recipients
        ]
