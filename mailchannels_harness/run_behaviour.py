"""OVERLORD-29 behaviour script, steps 2-4, on synthetic data only.

1. (Anymail's test suite and docs build run separately; see README.md.)
2. A sample Django project with EMAIL_BACKEND set to the MailChannels backend
   sends with send_mail, then an EmailMessage with an attachment. Each must
   reach the OpenAPI-validating stub as exactly one valid send-async request.
3. A signed synthetic delivery webhook posted to the tracking view must produce
   a normalized Anymail tracking signal.
4. A tampered webhook must be rejected (HTTP 400) without any signal.

Usage: python run_behaviour.py REPORT.json
Exits non-zero if any expectation fails.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from base64 import b64decode, b64encode
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent
PROJECT = HERE / "sampleproject"
STUB_PORT = 8765
DJANGO_PORT = 8766

results = []


def check(name, ok, detail=None):
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(
        ("PASS" if ok else "FAIL") + f": {name}" + (f" -- {detail}" if detail else "")
    )


def wait_for(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # server is answering
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not start")


def post(url, body, headers):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def read_jsonl(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def sign(private_key, body, created=None):
    """Sign like MailChannels: RFC 9421 Ed25519 over the body's Content-Digest.

    Follows https://docs.mailchannels.com/email-api/webhooks independently
    of Anymail's verifier.
    """
    created = int(time.time()) if created is None else created
    digest = "sha-256=:" + b64encode(hashlib.sha256(body).digest()).decode() + ":"
    params = f'("content-digest");created={created};alg="ed25519";keyid="mckey"'
    base = f'"content-digest": {digest}\n"@signature-params": {params}'.encode()
    signature = b64encode(private_key.sign(base)).decode()
    return {
        "Content-Type": "application/json",
        "Content-Digest": digest,
        "Signature-Input": f"sig_{created}={params}",
        "Signature": f"sig_{created}=:{signature}:",
    }


def main():
    report_path = Path(sys.argv[1]).resolve()
    work = Path(tempfile.mkdtemp(prefix="mc-harness-"))
    stub_log = work / "stub_requests.jsonl"
    tracking_log = work / "tracking_signals.jsonl"

    private_key = Ed25519PrivateKey.generate()  # synthetic signing key
    public_key = b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()

    env = dict(
        os.environ,
        STUB_API_URL=f"http://127.0.0.1:{STUB_PORT}/tx/v1/",
        WEBHOOK_PUBLIC_KEY=public_key,
        TRACKING_LOG=str(tracking_log),
        PYTHONUNBUFFERED="1",
    )
    processes = []
    try:
        stub = subprocess.Popen(
            [sys.executable, str(HERE / "stub_api.py"), str(STUB_PORT), str(stub_log)]
        )
        processes.append(stub)
        wait_for(f"http://127.0.0.1:{STUB_PORT}/__health")

        # Control: the stub really validates against the spec
        status, body = post(
            f"http://127.0.0.1:{STUB_PORT}/tx/v1/send-async",
            json.dumps({"subject": "no from or personalizations"}).encode(),
            {"Content-Type": "application/json", "X-Api-Key": "synthetic"},
        )
        check("stub rejects a request that violates the OpenAPI spec", status == 400)
        stub_log.unlink()

        def send(kind):
            before = len(read_jsonl(stub_log))
            output = subprocess.run(
                [sys.executable, "manage.py", "send_sample", kind],
                cwd=PROJECT,
                env=env,
                capture_output=True,
                text=True,
            )
            new = read_jsonl(stub_log)[before:]
            return output, new

        # Step 2a: unchanged send_mail
        output, requests = send("send_mail")
        check("send_mail returns 1", "sent=1" in output.stdout, output.stderr[-500:])
        check("send_mail made exactly one request", len(requests) == 1, len(requests))
        check(
            "send_mail request is valid for the OpenAPI spec",
            requests and all(r["valid"] and r["status"] == 202 for r in requests),
            requests and requests[0].get("errors"),
        )

        # Step 2b: EmailMessage with an attachment
        output, requests = send("attachment")
        check(
            "attachment send returns 1", "sent=1" in output.stdout, output.stderr[-500:]
        )
        check("attachment send made exactly one request", len(requests) == 1)
        request = requests[0] if requests else {"valid": False, "body": "{}"}
        check(
            "attachment request is valid for the OpenAPI spec",
            request["valid"],
            request.get("errors"),
        )
        payload = json.loads(request["body"])
        attachments = payload.get("attachments", [])
        check(
            "request carries the attachment",
            len(attachments) == 1
            and attachments[0]["filename"] == "invoice.csv"
            and b64decode(attachments[0]["content"]) == b"item,amount\nwidget,10.00\n",
            attachments and {k: v for k, v in attachments[0].items() if k != "content"},
        )
        check(
            "request uses the synthetic API key",
            request.get("headers", {}).get("X-Api-Key") == "synthetic-api-key",
        )

        # Steps 3 and 4: tracking webhook
        django = subprocess.Popen(
            [
                sys.executable,
                "manage.py",
                "runserver",
                f"127.0.0.1:{DJANGO_PORT}",
                "--noreload",
            ],
            cwd=PROJECT,
            env=env,
        )
        processes.append(django)
        webhook_url = f"http://127.0.0.1:{DJANGO_PORT}/anymail/mailchannels/tracking/"
        wait_for(webhook_url)

        event_body = json.dumps(
            [
                {
                    "email": "app@example.com",
                    "customer_handle": "synthetic",
                    "timestamp": int(time.time()),
                    "event": "delivered",
                    "request_id": "synthetic-request-id",
                    "smtp_id": "<synthetic@example.com>",
                    "recipients": ["customer@example.net"],
                }
            ]
        ).encode()
        status, _ = post(webhook_url, event_body, sign(private_key, event_body))
        signals = read_jsonl(tracking_log)
        check("signed webhook accepted (200)", status == 200, status)
        check(
            "signed webhook produced one normalized tracking signal",
            len(signals) == 1
            and signals[0]["esp_name"] == "MailChannels"
            and signals[0]["event_type"] == "delivered"
            and signals[0]["recipient"] == "customer@example.net"
            and signals[0]["message_id"] == "synthetic-request-id",
            signals,
        )

        headers = sign(private_key, event_body)
        tampered = event_body.replace(b'"delivered"', b'"hard-bounced"')
        status, _ = post(webhook_url, tampered, headers)
        check("tampered webhook rejected (400)", status == 400, status)
        check(
            "tampered webhook produced no tracking signal",
            len(read_jsonl(tracking_log)) == 1,
        )

        stale = sign(private_key, event_body, created=int(time.time()) - 3600)
        status, _ = post(webhook_url, event_body, stale)
        check("replayed (stale) webhook rejected (400)", status == 400, status)
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=10)

    report = {
        "passed": all(r["ok"] for r in results),
        "results": results,
        "stub_requests": read_jsonl(stub_log),
        "tracking_signals": read_jsonl(tracking_log),
    }
    report_path.write_text(json.dumps(report, indent=2))
    print("ALL PASSED" if report["passed"] else "FAILURES")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
