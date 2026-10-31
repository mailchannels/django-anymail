from django.core.mail import EmailMessage, send_mail
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send sample mail with Django's standard APIs (no Anymail-specific code)"

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=["send_mail", "attachment"])

    def handle(self, kind, **options):
        if kind == "send_mail":
            sent = send_mail(
                "Your receipt",
                "Thanks for your order.",
                "app@example.com",
                ["customer@example.net"],
                html_message="<p>Thanks for your order.</p>",
            )
        else:
            message = EmailMessage(
                "Your invoice",
                "Your invoice is attached.",
                "app@example.com",
                ["customer@example.net"],
            )
            message.attach("invoice.csv", "item,amount\nwidget,10.00\n", "text/csv")
            sent = message.send()
        self.stdout.write(f"sent={sent}")
