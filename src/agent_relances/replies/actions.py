"""Action proposée pour une réponse : décidée par le code, toujours confirmée par un humain."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from agent_emails.models import Email
from agent_emails.pipeline.fraud import KnownSenders, domain_of, fraud_signals, is_suspicious

from agent_relances.formatting import date_fr
from agent_relances.models import ActionKind, Plan, PlanStatus, ReplyIntent, ReplyReading
from agent_relances.replies.reader import promised_date_of

ANNOUNCED_PAYMENT_DAYS = 7
PROMISE_GRACE_DAYS = 3


@dataclass(frozen=True)
class ProposedAction:
    kind: ActionKind
    until: date | None
    installments: int | None
    reason: str


def reply_is_suspicious(email: Email, client_emails: Iterable[str], company_domain: str | None) -> bool:
    """Réponse frauduleuse probable : domaine sosie d'un client connu, lien trompeur, coordonnées bancaires, etc."""
    addresses = frozenset(a.strip().lower() for a in client_emails if a)
    domains = {domain_of(a) for a in addresses} | ({company_domain.lower()} if company_domain else set())
    known = KnownSenders(domains=frozenset(d for d in domains if d), addresses=addresses)
    return is_suspicious(fraud_signals(email, known), None)


def _human(reason: str) -> ProposedAction:
    return ProposedAction(ActionKind.HUMAN, None, None, reason)


def propose_action(reading: ReplyReading, suspicious: bool, has_unpaid: bool, open_plan: Plan | None,
                   today: date, has_dispute: bool = False) -> ProposedAction:
    if suspicious:
        return _human("Réponse suspecte (fraude possible) : aucune action automatique.")
    if has_dispute:
        return _human("Client en contentieux : aucune action automatique, le dossier suit son cours.")
    if not has_unpaid:
        return ProposedAction(ActionKind.NONE, None, None, "Le client n'a plus de facture impayée.")
    intent = reading.intent
    if intent is ReplyIntent.PAYMENT_ANNOUNCED:
        until = today + timedelta(days=ANNOUNCED_PAYMENT_DAYS)
        return ProposedAction(ActionKind.POSTPONE, until, None,
                              f"Paiement annoncé : relances suspendues jusqu'au {date_fr(until)}.")
    if intent is ReplyIntent.PROMISED_DATE:
        promised = promised_date_of(reading, today)
        if promised is None:
            return _human("Date de paiement promise absente, passée ou trop lointaine.")
        until = promised + timedelta(days=PROMISE_GRACE_DAYS)
        return ProposedAction(ActionKind.POSTPONE, until, None,
                              f"Paiement promis le {date_fr(promised)} : relances suspendues jusqu'au {date_fr(until)}.")
    if intent is ReplyIntent.PLAN_REQUEST:
        if open_plan is not None:
            return _human("Un échéancier est déjà ouvert pour ce client.")
        if reading.requested_installments == 1:
            return _human("Paiement demandé en une seule fois : ce n'est pas un échéancier.")
        return ProposedAction(ActionKind.PROPOSE_PLAN, None, reading.requested_installments,
                              "Le client demande à payer en plusieurs fois : proposer un échéancier.")
    if intent is ReplyIntent.PLAN_AGREEMENT:
        if open_plan is None or open_plan.status is not PlanStatus.PROPOSED:
            return _human("Accord reçu mais aucun échéancier proposé à activer.")
        return ProposedAction(ActionKind.ACTIVATE_PLAN, None, None, "Le client accepte l'échéancier proposé.")
    if intent is ReplyIntent.DISPUTE:
        return ProposedAction(ActionKind.SUSPEND, None, None,
                              "Contestation : relances suspendues jusqu'à décision d'un humain.")
    return _human("Réponse à lire et traiter par un humain.")
