.. _mailchannels-backend:

MailChannels
============

Anymail integrates with the `MailChannels Email API`_ for sending mail
and for status tracking through signed webhooks.
It sends with the `asynchronous send endpoint`_.

.. _MailChannels Email API: https://docs.mailchannels.com/email-api/
.. _asynchronous send endpoint:
    https://docs.mailchannels.com/api-reference/send/send-an-email-asynchronously

Installation
------------

Set your Django email backend and API key in settings.py::

    EMAIL_BACKEND = "anymail.backends.mailchannels.EmailBackend"
    ANYMAIL = {
        "MAILCHANNELS_API_KEY": "<your API key>",
    }

To verify :ref:`status tracking webhooks <mailchannels-webhooks>`,
install Anymail with the ``mailchannels`` extra, which adds the ``cryptography``
package:

.. code-block:: console

    $ python -m pip install 'django-anymail[mailchannels]'

Read the API key from your application's secret store. Sending requires an
active MailChannels Email API account, an authorized sender domain, and
`Domain Lockdown`_ configuration. Enabling open or click tracking also requires
an eligible subscription.

.. _Domain Lockdown: https://docs.mailchannels.com/email-api/domain-lockdown

.. setting:: ANYMAIL_MAILCHANNELS_API_KEY

.. rubric:: MAILCHANNELS_API_KEY

Required for sending. Anymail also looks for ``MAILCHANNELS_API_KEY`` at the
root of your settings if neither ``ANYMAIL["MAILCHANNELS_API_KEY"]`` nor
``ANYMAIL_MAILCHANNELS_API_KEY`` is set. You can pass ``api_key`` directly to
the backend constructor instead.

.. setting:: ANYMAIL_MAILCHANNELS_API_URL

.. rubric:: MAILCHANNELS_API_URL

The base API URL, defaulting to ``"https://api.mailchannels.net/tx/v1/"``.
Do not include ``send-async``. You can pass ``api_url`` directly to the backend
constructor. The normal Anymail :setting:`ANYMAIL_REQUESTS_TIMEOUT` applies.

.. setting:: ANYMAIL_MAILCHANNELS_WEBHOOK_PUBLIC_KEY

.. rubric:: MAILCHANNELS_WEBHOOK_PUBLIC_KEY

Required for status tracking webhooks. MailChannels' Ed25519 public key for
webhook signatures, as the base64 ``key`` value returned by its
`webhook public-key API`_ (a PEM public key also works)::

    ANYMAIL = {
        ...
        "MAILCHANNELS_WEBHOOK_PUBLIC_KEY": "<base64 key>",
    }

Fetch it once with the ``keyid`` from a webhook's ``Signature-Input`` header
(currently ``mckey``):

.. code-block:: console

    $ curl -H "X-Api-Key: $MAILCHANNELS_API_KEY" \
        "https://api.mailchannels.net/tx/v1/webhook/public-key?id=mckey"

The key is public, so it doesn't need to be kept secret. Anymail does not
fetch it for you, so update the setting if MailChannels rotates its key.
Anymail also looks for ``MAILCHANNELS_WEBHOOK_PUBLIC_KEY`` at the root of your
settings.

.. _webhook public-key API:
    https://docs.mailchannels.com/api-reference/webhooks/retrieve-webhook-signing-key

Sending behavior
----------------

Django's standard sending APIs work with this backend, including
``send_mail``, ``send_mass_mail``, ``EmailMessage``, ``EmailMultiAlternatives``,
reusable connections, display names, To/Cc/Bcc, a single Reply-To address,
text and HTML content, custom headers, and ordinary and inline attachments.
Attachment sizes, recipient limits, and other API policy limits are enforced
by MailChannels, not by this backend.

A successful send reports ``queued`` for every intended recipient and uses
MailChannels' ``request_id`` as :attr:`~anymail.message.AnymailStatus.message_id`.
This ID identifies the whole API request, including all personalizations.
It is not the RFC Message-ID header and is not unique per recipient. HTTP 202
means the request was queued; it does not prove per-recipient validation,
rendering, Domain Lockdown authorization, or delivery. Inspect MailChannels'
events and received messages for those outcomes.

API errors, malformed queue responses, and transport failures raise
:exc:`~anymail.exceptions.AnymailAPIError`. The backend does not retry sends.
A timeout can leave acceptance uncertain; retrying may duplicate mail.
Django's ``fail_silently`` behavior and Anymail's usual connection cleanup apply.

Templates and batch sending
---------------------------

MailChannels renders inline Mustache templates. Set
:attr:`~anymail.message.AnymailMessage.merge_global_data` and/or
:attr:`~anymail.message.AnymailMessage.merge_data` to enable Mustache rendering
of text and HTML content. Per-recipient values override global values::

    from anymail.message import AnymailMessage

    message = AnymailMessage(
        subject="Account update",
        body="Hello {{name}}",
        from_email="support@example.com",
        to=["alice@example.com", "bob@example.com"],
        merge_global_data={"name": "customer"},
        merge_data={"alice@example.com": {"name": "Alice"}},
    )
    message.send()

Setting ``merge_data`` (even ``{}``) or ``merge_headers`` makes a separate
personalization for each To recipient. Each retains the original Cc/Bcc list,
so those recipients receive a copy of every personalization. Global data alone
does not split the To list. ``merge_headers`` overrides root custom headers
per recipient according to MailChannels' case-insensitive header precedence.

Subject templating and stored ``template_id`` templates are not supported.

esp_extra support
-----------------

Use :attr:`~anymail.message.AnymailMessage.esp_extra` for MailChannels-specific
send options such as campaign IDs, DKIM settings, non-transactional mail, and
custom tracking domains::

    message.esp_extra = {
        "campaign_id": "account-update",
        "tracking_settings": {
            "click_tracking": {"custom_domain_name": "clicks"},
        },
    }

The dictionary is deeply merged into the request after Anymail's normalized
options. Lists replace generated lists, including ``personalizations``.
When replacing recipients through this escape hatch, the normalized recipient
status still describes the original Django recipients. Prefer normal ``to``,
``cc``, ``bcc``, and merge options when you need accurate recipient status.
You are responsible for the API's restrictions and any overridden values.

Limitations
-----------

* Each message needs at least one To recipient. Cc-only or Bcc-only messages
  cannot preserve their visible recipient semantics in this API.
* Only one Reply-To address is supported.
* MailChannels reserves certain headers, including Message-ID, To, Cc, Bcc,
  Subject, Content-Type, DKIM-Signature, and Received. These cannot be supplied
  as custom headers. Anymail maps From and Reply-To overrides to native API
  fields and rejects spoofed To headers. See the API reference for the full list.
* ``envelope_sender`` maps to ``envelope_from`` (the display name is ignored).
* ``track_clicks`` and ``track_opens`` map to the corresponding
  ``tracking_settings`` flags. Account eligibility still applies.
* A single Anymail ``tag`` is sent as MailChannels' ``campaign_id``, which
  its webhook events report back as the event's
  :attr:`~anymail.signals.AnymailTrackingEvent.tags`. Multiple tags are
  unsupported.
* Anymail ``metadata``, ``merge_metadata``, ``send_at``, AMP content,
  stored templates, and alternative bodies other than text/plain and text/html
  are unsupported. Metadata is not silently sent as visible recipient headers.
* Inline attachments need a Content-ID; normal attachments with a Content-ID
  remain normal attachments. MailChannels chooses inline disposition from its
  ``content_id`` field.
* MailChannels doesn't receive inbound mail, so there is no inbound webhook.

Unsupported features raise
:exc:`~anymail.exceptions.AnymailUnsupportedFeature` unless you explicitly
configure :ref:`unsupported-features` to ignore them.


.. _mailchannels-webhooks:

Status tracking webhooks
------------------------

If you are using Anymail's normalized :ref:`status tracking <event-tracking>`,
create a MailChannels webhook (on the `webhooks page`_ of the MailChannels
Console, or with the `create webhook API`_) with the URL:

   :samp:`https://{yoursite.example.com}/anymail/mailchannels/tracking/`

     * *yoursite.example.com* is your Django site

MailChannels signs every webhook call with an Ed25519
`HTTP message signature`_ (RFC 9421) over the body's ``Content-Digest``.
Anymail verifies the digest and the signature with
:setting:`MAILCHANNELS_WEBHOOK_PUBLIC_KEY <ANYMAIL_MAILCHANNELS_WEBHOOK_PUBLIC_KEY>`,
and rejects calls whose signature was created more than five minutes ago
(allowing 30 seconds of clock skew), so your server's clock must be accurate.
Because the calls are signed, Anymail's shared :setting:`ANYMAIL_WEBHOOK_SECRET`
basic authentication is optional for this webhook (it's still checked if you
set it and include the credentials in the webhook URL).

MailChannels' events are reported as these Anymail
:attr:`~anymail.signals.AnymailTrackingEvent.event_type`\s:

==================  ========================  ==================
MailChannels        Anymail ``event_type``    ``reject_reason``
==================  ========================  ==================
``processed``       queued
``delivered``       delivered
``dropped``         rejected                  other
``hard-bounced``    bounced                   bounced
``soft-bounced``    deferred
``open``            opened
``click``           clicked
``complained``      complained                spam
``unsubscribed``    unsubscribed              unsubscribed
``test``, other     unknown
==================  ========================  ==================

A MailChannels event can list several ``recipients``; Anymail sends a separate
tracking signal for each one, with the same
:attr:`~anymail.signals.AnymailTrackingEvent.esp_event`. Events without
recipients (such as ``processed``) have a ``None``
:attr:`~anymail.signals.AnymailTrackingEvent.recipient`.

The event's :attr:`~anymail.signals.AnymailTrackingEvent.message_id` is the
send API's ``request_id``, matching the message's
:attr:`~anymail.message.AnymailStatus.message_id` when it was sent. (It is
``None`` for mail sent through MailChannels SMTP.) MailChannels doesn't provide
a unique ID for each event, so :attr:`~anymail.signals.AnymailTrackingEvent.event_id`
is ``None``. :attr:`~anymail.signals.AnymailTrackingEvent.tags` holds the
message's ``campaign_id``, if any. For bounces and drops,
:attr:`~anymail.signals.AnymailTrackingEvent.description` is MailChannels'
``reason`` and :attr:`~anymail.signals.AnymailTrackingEvent.mta_response`
combines its SMTP ``status`` and ``reason``. Click events include
:attr:`~anymail.signals.AnymailTrackingEvent.click_url`, and click and open
events include :attr:`~anymail.signals.AnymailTrackingEvent.user_agent`.

The event's :attr:`~anymail.signals.AnymailTrackingEvent.esp_event` field is
the `dict` of MailChannels' `webhook event data`_, including fields Anymail
doesn't normalize, such as ``customer_handle``, ``smtp_id`` and ``ip``.
If you send for several MailChannels sub-accounts, check ``customer_handle``.

.. _webhooks page: https://dash.mailchannels.com/webhooks
.. _create webhook API:
    https://docs.mailchannels.com/api-reference/webhooks/enroll-for-webhook-notifications
.. _HTTP message signature:
    https://docs.mailchannels.com/email-api/webhooks#verifying-message-signatures
.. _webhook event data: https://docs.mailchannels.com/email-api/webhook-event-types
