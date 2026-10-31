import json

from django.apps import AppConfig
from django.conf import settings


def record_tracking_event(sender, event, esp_name, **kwargs):
    with open(settings.TRACKING_LOG, "a") as log:
        log.write(
            json.dumps(
                {
                    "esp_name": esp_name,
                    "event_type": event.event_type,
                    "recipient": event.recipient,
                    "message_id": event.message_id,
                    "timestamp": event.timestamp.isoformat(),
                    "tags": event.tags,
                    "mta_response": event.mta_response,
                }
            )
            + "\n"
        )


class SampleAppConfig(AppConfig):
    name = "sampleapp"

    def ready(self):
        from anymail.signals import tracking

        tracking.connect(record_tracking_event)
