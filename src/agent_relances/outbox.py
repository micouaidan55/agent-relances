"""Dépôt d'une relance validée sous forme de brouillon .eml. Rien n'est jamais envoyé."""

from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from agent_relances.models import Client


def reference(kind: str, number: int) -> str:
    """Référence portée dans l'objet des mails déposés : R-00042 (relance), E-00007 (échéancier)."""
    return f"{kind}-{number:05d}"


def with_reference(subject: str, ref: str) -> str:
    """Ajoute « [Réf. R-00042] » à la fin de l'objet, une seule fois."""
    tag = f"[Réf. {ref}]"
    subject = " ".join(subject.split())
    return subject if subject.endswith(tag) else f"{subject} {tag}"


def save_reminder_eml(outbox: Path, reminder_id: int, client: Client, subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = formataddr((client.contact, client.email)) if client.contact else client.email
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["X-Unsent"] = "1"  # les clients mail ouvrent le fichier comme un brouillon modifiable
    message["X-Agent-Relances"] = f"relance-{reminder_id}"
    message.set_content(body)
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"relance_{reminder_id:05d}.eml"
    path.write_bytes(message.as_bytes())
    return str(path)
