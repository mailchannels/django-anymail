"""Stub MailChannels Email API that validates requests against the OpenAPI spec.

Only for the OVERLORD-29 harness: it accepts POST /tx/v1/send-async, validates
the request (path, X-Api-Key header and JSON body) against email-api.yaml, and
records each request in a JSON-lines log. Valid requests get the documented
202 AsyncSendResponse; invalid ones get 400 with the validation errors.

Usage: python stub_api.py PORT LOGFILE
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

import yaml
from openapi_core import OpenAPI
from openapi_core.contrib.werkzeug import WerkzeugOpenAPIRequest
from werkzeug.wrappers import Request, Response

SPEC_PATH = Path(__file__).with_name("email-api.yaml")


def load_openapi(port):
    spec = yaml.safe_load(SPEC_PATH.read_text())
    # Validate requests made to this stub as if made to the real server
    spec["servers"] = [{"url": f"http://127.0.0.1:{port}/tx/v1"}]
    return OpenAPI.from_dict(spec)


def make_app(openapi, log_path):
    @Request.application
    def app(request):
        if request.path == "/__health":
            return Response("ok")
        record = {
            "method": request.method,
            "path": request.path,
            "headers": dict(request.headers),
            "body": request.get_data(as_text=True),
        }
        try:
            openapi.validate_request(WerkzeugOpenAPIRequest(request))
        except Exception as error:  # report every validation failure to the caller
            errors = [str(error)] + [
                str(detail) for detail in getattr(error, "schema_errors", [])
            ]
            record["valid"] = False
            record["errors"] = errors
            status, body = 400, {"errors": errors}
        else:
            if request.path != "/tx/v1/send-async":
                record["valid"] = False
                status, body = 404, {"errors": ["stub only implements send-async"]}
            else:
                record["valid"] = True
                status, body = 202, {
                    "request_id": uuid.uuid4().hex,
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
        record["status"] = status
        with open(log_path, "a") as log:
            log.write(json.dumps(record) + "\n")
        return Response(json.dumps(body), status=status, mimetype="application/json")

    return app


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("stub_api: " + (format % args) + "\n")


def main():
    port, log_path = int(sys.argv[1]), sys.argv[2]
    server = make_server(
        "127.0.0.1",
        port,
        make_app(load_openapi(port), log_path),
        handler_class=QuietHandler,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
