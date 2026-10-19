"""Dépôt d'une relance validée sous forme de brouillon .eml. Rien n'est jamais envoyé."""

import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

from agent_relances.models import Client

_REF_TAG = re.compile(r"\s*\[Réf\. [A-Z]-\d+\]")


def reference(kind: str, number: int) -> str:
    """Référence portée dans l'objet des mails déposés : R-00042 (relance), E-00007 (échéancier)."""
    return f"{kind}-{number:05d}"


def with_reference(subject: str, ref: str) -> str:
    """Place « [Réf. R-00042] » à la fin de l'objet, une seule fois : toute autre référence est retirée."""
    subject = " ".join(_REF_TAG.sub("", subject).split())
    return f"{subject} [Réf. {ref}]"


def _write_eml(outbox: Path, filename: str, marker: str, client: Client, subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = formataddr((client.contact, client.email)) if client.contact else client.email
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["X-Unsent"] = "1"  # les clients mail ouvrent le fichier comme un brouillon modifiable
    message["X-Agent-Relances"] = marker
    message.set_content(body)
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / filename
    path.write_bytes(message.as_bytes())
    return str(path)


def save_reminder_eml(outbox: Path, reminder_id: int, client: Client, subject: str, body: str) -> str:
    return _write_eml(outbox, f"relance_{reminder_id:05d}.eml", f"relance-{reminder_id}", client, subject, body)


def plan_eml_path(outbox: Path, plan_id: int, suffix: str = "") -> Path:
    return Path(outbox) / f"echeancier_{plan_id:05d}{suffix}.eml"


def save_plan_eml(outbox: Path, plan_id: int, client: Client, subject: str, body: str, suffix: str = "") -> str:
    return _write_eml(outbox, plan_eml_path(outbox, plan_id, suffix).name, f"echeancier-{plan_id}", client,
                      subject, body)
