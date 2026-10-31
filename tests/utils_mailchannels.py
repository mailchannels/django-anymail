import hashlib
from base64 import b64encode
from time import time

from django.test import override_settings

from tests.utils import ClientWithCsrfChecks

HAS_CRYPTOGRAPHY = True
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    HAS_CRYPTOGRAPHY = False


def make_key():
    """Generate an Ed25519 private key, for testing only"""
    return Ed25519PrivateKey.generate()


def derive_public_webhook_key(private_key):
    """Return the public key in the base64 format of MailChannels' public-key API"""
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return b64encode(raw).decode("ascii")


def derive_public_pem(private_key):
    """Return the public key in PEM (SubjectPublicKeyInfo) format"""
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def sign_headers(
    private_key, body, created=None, label="sig_1738775282", alg="ed25519"
):
    """Return the headers MailChannels adds to sign a webhook body

    (Content-Digest, Signature-Input and Signature, following RFC 9421
    and https://docs.mailchannels.com/email-api/webhooks.)
    """
    if created is None:
        created = int(time())
    content_digest = "sha-256=:%s:" % b64encode(hashlib.sha256(body).digest()).decode(
        "ascii"
    )
    signature_params = '("content-digest");created=%d;alg="%s";keyid="mckey"' % (
        created,
        alg,
    )
    signature_base = (
        '"content-digest": %s\n"@signature-params": %s'
        % (content_digest, signature_params)
    ).encode("utf-8")
    signature = b64encode(private_key.sign(signature_base)).decode("ascii")
    return {
        "Content-Digest": content_digest,
        "Signature-Input": "%s=%s" % (label, signature_params),
        "Signature": "%s=:%s:" % (label, signature),
    }


class _ClientWithMailChannelsSignature(ClientWithCsrfChecks):
    private_key = None

    def set_private_key(self, private_key):
        self.private_key = private_key

    def post(self, *args, **kwargs):
        data = kwargs["data"]
        body = data.encode("utf-8") if isinstance(data, str) else data
        headers = kwargs.get("headers", {})
        for name, value in sign_headers(self.private_key, body).items():
            headers.setdefault(name, value)
        kwargs["headers"] = headers

        webhook_key = derive_public_webhook_key(self.private_key)
        with override_settings(
            ANYMAIL={"MAILCHANNELS_WEBHOOK_PUBLIC_KEY": webhook_key}
        ):
            return super().post(*args, **kwargs)


ClientWithMailChannelsSignature = (
    _ClientWithMailChannelsSignature if HAS_CRYPTOGRAPHY else None
)
