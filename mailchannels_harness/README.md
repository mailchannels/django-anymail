# MailChannels ESP harness (fork only; not part of the upstream pull request)

This folder exists only on the `agent/overlord-29/sample-project-ci` branch of
`mailchannels/django-anymail`. It proves the MailChannels ESP for
[OVERLORD-29](https://mailchannels.atlassian.net/browse/OVERLORD-29) end to end
on synthetic data. Don't include it in the upstream pull request, which comes
from `agent/overlord-29/mailchannels-esp`.

- `stub_api.py`: a stub MailChannels Email API that validates every request
  against `email-api.yaml` (the published OpenAPI spec, copied from
  `mailchannels/mc-docs` at commit `091028b3`, 2026-10-29) and answers
  `send-async` with the documented 202 response.
- `sampleproject/`: a minimal Django project. Its only email configuration is
  `EMAIL_BACKEND = "anymail.backends.mailchannels.EmailBackend"` plus the
  `ANYMAIL` settings; `sampleapp` sends with Django's own `send_mail` and
  `EmailMessage` and records Anymail tracking signals.
- `run_behaviour.py`: runs the behaviour script steps 2-4 (send through the
  stub, signed webhook to a normalized signal, tampered and stale webhooks
  rejected) and writes a JSON report.

Run locally from the repository root:

```sh
python -m pip install -e . -r mailchannels_harness/requirements.txt
PYTHONPATH=$PWD python mailchannels_harness/run_behaviour.py behaviour-report.json
```

`.github/workflows/mailchannels-harness.yml` runs it in CI, together with
Anymail's tox environments for the MailChannels tests, lint and docs.
