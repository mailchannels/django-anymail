import json
import unittest
from base64 import b64encode
from datetime import datetime, timezone
from time import time
from unittest.mock import ANY

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings, tag

from anymail.exceptions import AnymailConfigurationError
from anymail.signals import AnymailTrackingEvent
from anymail.webhooks.mailchannels import MailChannelsTrackingWebhookView

from .utils_mailchannels import (
    ClientWithMailChannelsSignature,
    derive_public_pem,
    derive_public_webhook_key,
    make_key,
    sign_headers,
)
from .webhook_cases import WebhookTestCase

WEBHOOK_URL = "/anymail/mailchannels/tracking/"


@tag("mailchannels")
@unittest.skipUnless(
    ClientWithMailChannelsSignature,
    "Install 'cryptography' to run MailChannels webhook tests",
)
class MailChannelsWebhookSecurityTestCase(WebhookTestCase):
    client_class = ClientWithMailChannelsSignature

    def setUp(self):
        super().setUp()
        self.clear_basic_auth()
        self.private_key = make_key()
        self.client.set_private_key(self.private_key)
        self.body = json.dumps(
            [
                {
                    "email": "sender@example.com",
                    "customer_handle": "abc123",
                    "timestamp": 1625098000,
                    "event": "delivered",
                    "request_id": "wBLWrCnK0Z965pf-cgxhNg8bo5s=",
                    "recipients": ["recipient@example.net"],
                }
            ]
        )

    def post(self, data=None, headers=None):
        return self.client.post(
            WEBHOOK_URL,
            content_type="application/json",
            data=self.body if data is None else data,
            headers=headers or {},
        )

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.tracking_handler.call_count, 0)

    def test_verifies_correct_signature(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tracking_handler.call_count, 1)

    def test_basic_auth_not_needed(self):
        # (Signed webhooks don't warn about missing basic auth)
        self.post()  # WebhookTestCase fails on any AnymailInsecureWebhookWarning

    def test_tampered_body(self):
        # Signed headers for the original body, posted with a modified body
        headers = sign_headers(self.private_key, self.body.encode("utf-8"))
        tampered = self.body.replace("delivered", "hard-bounced")
        self.assert_rejected(self.post(data=tampered, headers=headers))

    def test_tampered_body_with_recomputed_digest(self):
        # Attacker recomputes Content-Digest for a modified body,
        # but can't produce a matching signature
        original = sign_headers(self.private_key, self.body.encode("utf-8"))
        tampered = self.body.replace("delivered", "hard-bounced")
        forged = sign_headers(make_key(), tampered.encode("utf-8"))
        forged["Signature"] = original["Signature"]
        forged["Signature-Input"] = original["Signature-Input"]
        self.assert_rejected(self.post(data=tampered, headers=forged))

    def test_signed_by_wrong_key(self):
        headers = sign_headers(make_key(), self.body.encode("utf-8"))
        self.assert_rejected(self.post(headers=headers))

    def test_tampered_signature_params(self):
        # Changing any signed parameter (here, created) invalidates the signature
        headers = sign_headers(self.private_key, self.body.encode("utf-8"))
        created = int(time())
        headers["Signature-Input"] = (
            'sig_1738775282=("content-digest");created=%d;alg="ed25519";keyid="other"'
            % created
        )
        self.assert_rejected(self.post(headers=headers))

    def test_unquoted_signature_params_not_equivalent(self):
        # @signature-params is the Signature-Input value as sent, quotes included
        headers = sign_headers(self.private_key, self.body.encode("utf-8"))
        headers["Signature-Input"] = headers["Signature-Input"].replace(
            '"ed25519"', "ed25519"
        )
        self.assert_rejected(self.post(headers=headers))

    def test_expired_signature(self):
        headers = sign_headers(
            self.private_key, self.body.encode("utf-8"), created=int(time()) - 600
        )
        self.assert_rejected(self.post(headers=headers))

    def test_future_signature(self):
        headers = sign_headers(
            self.private_key, self.body.encode("utf-8"), created=int(time()) + 600
        )
        self.assert_rejected(self.post(headers=headers))

    def test_signature_must_cover_content_digest(self):
        created = int(time())
        params = '();created=%d;alg="ed25519";keyid="mckey"' % created
        signature = self.private_key.sign(
            ('"@signature-params": %s' % params).encode("utf-8")
        )
        headers = sign_headers(self.private_key, self.body.encode("utf-8"))
        headers["Signature-Input"] = "sig1=" + params
        headers["Signature"] = "sig1=:%s:" % b64encode(signature).decode("ascii")
        self.assert_rejected(self.post(headers=headers))

    def test_unsupported_algorithm(self):
        # (Validly signed, but claims an algorithm Anymail doesn't implement)
        headers = sign_headers(
            self.private_key, self.body.encode("utf-8"), alg="rsa-pss-sha512"
        )
        self.assert_rejected(self.post(headers=headers))

    def test_signature_label_mismatch(self):
        headers = sign_headers(self.private_key, self.body.encode("utf-8"))
        headers["Signature"] = headers["Signature"].replace("sig_1738775282", "sig2")
        self.assert_rejected(self.post(headers=headers))

    def test_missing_or_garbage_headers(self):
        for name in ["Content-Digest", "Signature-Input", "Signature"]:
            for value in ["", "garbage", "sig_1738775282=:not base64!:"]:
                with self.subTest(header=name, value=value):
                    self.assert_rejected(self.post(headers={name: value}))

    def test_public_key_formats(self):
        pem = derive_public_pem(self.private_key)
        for key in [derive_public_webhook_key(self.private_key), pem]:
            with self.subTest(key=key[:20]):
                view = MailChannelsTrackingWebhookView(webhook_public_key=key)
                self.assertIsNotNone(view.verification_key)
        for key in [
            "",
            "not base64",
            "c2hvcnQ=",
            "ключ",
            "-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----",
        ]:
            with self.subTest(key=key):
                with self.assertRaises(AnymailConfigurationError):
                    MailChannelsTrackingWebhookView(webhook_public_key=key)


@tag("mailchannels")
@unittest.skipUnless(
    ClientWithMailChannelsSignature,
    "Install 'cryptography' to run MailChannels webhook tests",
)
class MailChannelsTrackingTestCase(WebhookTestCase):
    client_class = ClientWithMailChannelsSignature

    def setUp(self):
        super().setUp()
        self.clear_basic_auth()
        self.client.set_private_key(make_key())

    def post_events(self, esp_events):
        response = self.client.post(
            WEBHOOK_URL, content_type="application/json", data=json.dumps(esp_events)
        )
        self.assertEqual(response.status_code, 200)

    def get_event(self, esp_event):
        self.post_events([esp_event])
        kwargs = self.assert_handler_called_once_with(
            self.tracking_handler,
            sender=MailChannelsTrackingWebhookView,
            event=ANY,
            esp_name="MailChannels",
        )
        event = kwargs["event"]
        self.assertIsInstance(event, AnymailTrackingEvent)
        self.assertEqual(event.esp_event, esp_event)
        return event

    def test_delivered_event(self):
        event = self.get_event(
            {
                "email": "sender@example.com",
                "customer_handle": "abc123",
                "timestamp": 1625098000,
                "event": "delivered",
                "request_id": "wBLWrCnK0Z965pf-cgxhNg8bo5s=",
                "smtp_id": "<c8d0c0e8412a4e9e9687814d9a7c67da@example.com>",
                "recipients": ["recipient@example.net"],
            }
        )
        self.assertEqual(event.event_type, "delivered")
        self.assertEqual(
            event.timestamp, datetime(2021, 7, 1, 0, 6, 40, tzinfo=timezone.utc)
        )
        self.assertEqual(event.message_id, "wBLWrCnK0Z965pf-cgxhNg8bo5s=")
        self.assertIsNone(event.event_id)
        self.assertEqual(event.recipient, "recipient@example.net")
        self.assertEqual(event.tags, [])
        self.assertIsNone(event.reject_reason)
        self.assertIsNone(event.mta_response)

    def test_processed_event(self):
        event = self.get_event(
            {
                "email": "sender@example.com",
                "customer_handle": "abc123",
                "timestamp": 1625097600,
                "event": "processed",
                "request_id": "wBLWrCnK0Z965pf-cgxhNg8bo5s=",
            }
        )
        self.assertEqual(event.event_type, "queued")
        self.assertIsNone(event.recipient)

    def test_hard_bounced_event_multiple_recipients(self):
        # Example from https://docs.mailchannels.com/email-api/hard-bounced-events
        esp_event = {
            "email": "sender@example.com",
            "customer_handle": "examplecorp",
            "timestamp": 1700000000,
            "event": "hard-bounced",
            "recipients": ["recipient@example.net", "recipient2@example.net"],
            "status": "521",
            "reason": "Server does not accept mail",
            "campaign_id": "spring-sale-2026",
            "request_id": "4b06083a23724f279c8d11a370e641f8",
            "smtp_id": "<c8d0c0e8412a4e9e9687814d9a7c67da@example.com>",
        }
        self.post_events([esp_event])
        self.assertEqual(self.tracking_handler.call_count, 2)
        events = [call.kwargs["event"] for call in self.tracking_handler.call_args_list]
        self.assertEqual(
            [event.recipient for event in events],
            ["recipient@example.net", "recipient2@example.net"],
        )
        for event in events:
            self.assertEqual(event.event_type, "bounced")
            self.assertEqual(event.reject_reason, "bounced")
            self.assertEqual(event.message_id, "4b06083a23724f279c8d11a370e641f8")
            self.assertEqual(event.description, "Server does not accept mail")
            self.assertEqual(event.mta_response, "521 Server does not accept mail")
            self.assertEqual(event.tags, ["spring-sale-2026"])
            self.assertEqual(event.esp_event, esp_event)

    def test_soft_bounced_event(self):
        event = self.get_event(
            {
                "customer_handle": "examplecorp",
                "timestamp": 1700000000,
                "event": "soft-bounced",
                "recipients": ["recipient@example.net"],
                "status": "451",
                "reason": "Try again later",
                "request_id": "4b06083a23724f279c8d11a370e641f8",
            }
        )
        self.assertEqual(event.event_type, "deferred")
        self.assertIsNone(event.reject_reason)
        self.assertEqual(event.mta_response, "451 Try again later")

    def test_dropped_event(self):
        event = self.get_event(
            {
                "customer_handle": "examplecorp",
                "timestamp": 1700000000,
                "event": "dropped",
                "recipients": ["recipient@example.net"],
                "reason": "Recipient is suppressed",
                "request_id": "4b06083a23724f279c8d11a370e641f8",
            }
        )
        self.assertEqual(event.event_type, "rejected")
        self.assertEqual(event.reject_reason, "other")
        self.assertEqual(event.description, "Recipient is suppressed")

    def test_click_event(self):
        event = self.get_event(
            {
                "email": "sender@example.com",
                "customer_handle": "examplecorp",
                "timestamp": 1700000000,
                "event": "click",
                "recipients": ["recipient@example.net"],
                "url": "https://example.com/offer",
                "user_agent": "Mozilla/5.0",
                "ip": "192.0.2.1",
                "request_id": "4b06083a23724f279c8d11a370e641f8",
                "smtp_id": "<c8d0c0e8412a4e9e9687814d9a7c67da@example.com>",
            }
        )
        self.assertEqual(event.event_type, "clicked")
        self.assertEqual(event.click_url, "https://example.com/offer")
        self.assertEqual(event.user_agent, "Mozilla/5.0")

    def test_open_event(self):
        event = self.get_event(
            {
                "customer_handle": "examplecorp",
                "timestamp": 1700000000,
                "event": "open",
                "user_agent": "Mozilla/5.0",
                "request_id": "4b06083a23724f279c8d11a370e641f8",
            }
        )
        self.assertEqual(event.event_type, "opened")
        self.assertEqual(event.user_agent, "Mozilla/5.0")

    def test_complained_and_unsubscribed_events(self):
        for esp_type, event_type, reject_reason in [
            ("complained", "complained", "spam"),
            ("unsubscribed", "unsubscribed", "unsubscribed"),
        ]:
            with self.subTest(esp_type):
                self.tracking_handler.reset_mock()
                event = self.get_event(
                    {
                        "customer_handle": "examplecorp",
                        "timestamp": 1700000000,
                        "event": esp_type,
                        "recipients": ["recipient@example.net"],
                        "request_id": "",  # blank for messages sent with SMTP
                    }
                )
                self.assertEqual(event.event_type, event_type)
                self.assertEqual(event.reject_reason, reject_reason)
                self.assertEqual(event.recipient, "recipient@example.net")
                self.assertIsNone(event.message_id)

    def test_test_and_unknown_events(self):
        for esp_type in ["test", "some-future-event"]:
            with self.subTest(esp_type):
                self.tracking_handler.reset_mock()
                event = self.get_event(
                    {
                        "email": "test@mailchannels.com",
                        "customer_handle": "abc123",
                        "timestamp": 1700000000,
                        "event": esp_type,
                        "request_id": "test-request",
                        "smtp_id": "<test@mailchannels.net>",
                    }
                )
                self.assertEqual(event.event_type, "unknown")

    def test_batch_of_events(self):
        self.post_events(
            [
                {"customer_handle": "abc", "timestamp": 1, "event": "processed"},
                {"customer_handle": "abc", "timestamp": 2, "event": "delivered"},
            ]
        )
        self.assertEqual(
            [
                call.kwargs["event"].event_type
                for call in self.tracking_handler.call_args_list
            ],
            ["queued", "delivered"],
        )


@tag("mailchannels")
class MailChannelsWebhookSettingsTestCase(WebhookTestCase):
    @override_settings(ANYMAIL={})
    def test_requires_public_key(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured, "MAILCHANNELS_WEBHOOK_PUBLIC_KEY"
        ):
            MailChannelsTrackingWebhookView()
