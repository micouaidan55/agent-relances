"""Rattachement des réponses des clients : par référence de l'objet, sinon par adresse de l'expéditeur."""

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent_emails.connectors.folder import FolderSource
from agent_emails.models import Email

from agent_relances.models import client_key
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore

REFERENCE = re.compile(r"\[Réf\. ([RE])-(\d{5})\]")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchResult:
    client_key: str | None
    reference: str | None
    method: str | None  # "reference", "expediteur" ou None


def match_reply(email: Email, reminders: ReminderStore, recovery: RecoveryStore) -> MatchResult:
    """Référence R (relance) ou E (échéancier) d'abord ; une référence inconnue retombe sur l'expéditeur."""
    found = REFERENCE.search(email.subject or "")
    if found:
        kind, number = found.group(1), int(found.group(2))
        if kind == "R":
            reminder = reminders.get(number)
            key = reminder.client_key if reminder else None
        else:
            plan = recovery.get_plan(number)
            key = plan.client_key if plan else None
        if key:
            return MatchResult(client_key(key), f"{kind}-{number:05d}", "reference")
    sender = (email.sender_address or "").strip().casefold()
    if sender:
        for client in reminders.list_clients():
            if client.email.strip().casefold() == sender:
                return MatchResult(client.key, None, "expediteur")
    return MatchResult(None, None, None)


def fetch_replies(inbox: Path, reminders: ReminderStore, recovery: RecoveryStore) -> list[tuple[Email, MatchResult]]:
    """Nouveaux emails rattachés à un client ou portant une référence (même inconnue) ; les autres sont laissés à agent-emails. Emails illisibles : journalisés."""
    with tempfile.TemporaryDirectory() as unused_drafts:  # fetch_new n'écrit jamais de brouillon
        result = FolderSource(inbox, Path(unused_drafts)).fetch_new(recovery.known_reply_ids())
    for name, error in result.errors:
        log.warning("Email illisible ignoré (%s) : %s", name, error)
    matched = [(email, match_reply(email, reminders, recovery)) for email in result.emails]
    return [(email, match) for email, match in matched
            if match.client_key is not None or REFERENCE.search(email.subject or "")]
