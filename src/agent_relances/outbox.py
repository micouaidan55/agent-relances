"""Dépôt d'une relance validée sous forme de brouillon .eml. Rien n'est jamais envoyé."""

from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from agent_relances.models import Client


def save_reminder_eml(outbox: Path, reminder_id: int, client: Client, subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = formataddr((client.contact, client.email)) if client.contact else client.email
    message["Subject"] = subject
    message["X-Agent-Relances"] = f"relance-{reminder_id}"
    message.set_content(body)
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"relance_{reminder_id:05d}.eml"
    path.write_bytes(message.as_bytes())
    return str(path)
