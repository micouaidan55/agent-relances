"""Traitement des réponses des clients : comprendre et proposer d'abord, appliquer seulement après confirmation humaine."""

import logging
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import anthropic
from agent_emails.models import Email

from agent_relances.drafting.llm import StepError
from agent_relances.drafting.plans import CONFIRMATION_SUFFIX, confirmation_draft, deposit_plan_draft, proposal_draft
from agent_relances.formatting import date_fr
from agent_relances.models import (
    ActionKind, OverdueInvoice, Plan, PlanStatus, ReplyIntent, ReplyReading, ReplyStatus, Usage,
)
from agent_relances.replies.actions import propose_action, reply_is_suspicious
from agent_relances.replies.matching import fetch_replies
from agent_relances.replies.reader import promised_date_of, read_reply
from agent_relances.outbox import plan_eml_path
from agent_relances.rules.installments import PlanRuleError, max_installments, plan_amounts, plan_violations
from agent_relances.sources import InvoiceSource
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore

log = logging.getLogger(__name__)
_CENT = Decimal("0.01")


def process_replies(inbox: Path, reminders: ReminderStore, recovery: RecoveryStore, source: InvoiceSource, api,
                    company: str, company_domain: str, today: date) -> list[int]:
    """Lit les nouvelles réponses, les rattache, les comprend et enregistre l'action proposée (rien n'est appliqué)."""
    stored_ids: list[int] = []
    client_emails = [c.email for c in reminders.list_clients()]  # toutes les fiches : les sosies visent les clients
    for email, match in fetch_replies(inbox, reminders, recovery):
        common = dict(
            message_id=email.message_id, client_key=match.client_key, reminder_ref=match.reference,
            match_method=match.method or "aucune", sender=email.sender_address, subject=email.subject,
            received_at=email.received_at.isoformat(), body=email.body, today=today,
        )
        suspicious = reply_is_suspicious(email, client_emails, company_domain)  # chaque réponse, rattachée ou non
        if match.client_key is None:
            fields = dict(intent=ReplyIntent.OTHER, promised_date=None, requested_installments=None,
                          summary="Réponse non rattachée à un client.", suspicious=suspicious,
                          proposed_action=ActionKind.NONE, action_until=None, status=ReplyStatus.UNMATCHED,
                          cost_usd=0.0)
        else:
            fields = _analyse(email, match.client_key, suspicious, recovery, source, api, company, today)
        try:
            reply_id = recovery.add_reply(**common, **fields)
        except ValueError as error:
            log.warning("Réponse ignorée : %s", error)
            continue
        log.info("Réponse %d de %s : %s -> %s", reply_id, email.sender_address, fields["intent"].value,
                 fields["proposed_action"].value)
        reminders.log(None, "reponse_enregistree", {"reponse": reply_id, "client": match.client_key,
                                                    "intention": fields["intent"].value,
                                                    "action": fields["proposed_action"].value})
        stored_ids.append(reply_id)
    return stored_ids


def _analyse(email, key: str, suspicious: bool, recovery: RecoveryStore, source: InvoiceSource, api, company: str,
             today: date) -> dict:
    """Lecture par le modèle et action proposée par le code pour une réponse rattachée au client `key`."""
    reading, usage = _read(api, email, company, today)
    unpaid = source.unpaid_of(key, today)
    if reading is None:  # « autre » : le code propose toujours un humain
        reading = ReplyReading(intent=ReplyIntent.OTHER, summary="Réponse non comprise par le modèle.")
    has_dispute = any(d["client_key"] == key for d in recovery.list_disputes())
    action = propose_action(reading, suspicious, bool(unpaid), recovery.open_plan_for(key), today, has_dispute)
    return dict(intent=reading.intent, promised_date=promised_date_of(reading, today),
                requested_installments=reading.requested_installments, summary=reading.summary,
                suspicious=suspicious, proposed_action=action.kind, action_until=action.until,
                status=ReplyStatus.TO_REVIEW, cost_usd=usage.cost_usd)


def attach_reply(reply_id: int, key: str, reminders: ReminderStore, recovery: RecoveryStore, source: InvoiceSource,
                 api, company: str, company_domain: str, today: date) -> list[str]:
    """« Rattacher à… » : l'humain choisit le client d'une réponse non rattachée. Elle est relue (si une clé d'API est
    présente), recontrôlée pour la fraude et reçoit son action proposée, puis passe « à traiter ». Rien n'est
    appliqué. Renvoie les erreurs (vide si succès)."""
    stored = recovery.get_reply(reply_id)
    if stored is None or stored["status"] is not ReplyStatus.UNMATCHED:
        return ["Cette réponse n'est plus à rattacher."]
    card = reminders.get_client(key)
    if card is None:
        return ["Fiche client introuvable."]
    email = Email(message_id=stored["message_id"], sender_address=stored["sender"], subject=stored["subject"],
                  received_at=datetime.fromisoformat(stored["received_at"]), body=stored["body"])
    # Le drapeau enregistré à la réception (qui voyait les liens et l'adresse de réponse) est conservé.
    suspicious = stored["suspicious"] or reply_is_suspicious(email, [c.email for c in reminders.list_clients()],
                                                             company_domain)
    fields = _analyse(email, card.key, suspicious, recovery, source, api, company, today)
    del fields["status"]
    if not recovery.attach_reply(reply_id, card.key, **fields):
        return ["Cette réponse vient d'être rattachée ou traitée."]
    reminders.log(None, "reponse_rattachee", {"reponse": reply_id, "client": card.key,
                                              "action": fields["proposed_action"].value})
    log.info("Réponse %d rattachée à %s : %s", reply_id, card.key, fields["proposed_action"].value)
    return []


def _read(api, email, company: str, today: date) -> tuple[ReplyReading | None, Usage]:
    if api is None:
        return None, Usage()
    try:
        return read_reply(api, email, company, today)
    except StepError as error:
        log.warning("Réponse %s non comprise : %s", email.message_id, error)
        return None, error.usage
    except (anthropic.APIError, RuntimeError) as error:
        log.warning("Réponse %s non comprise : %s", email.message_id, error)
        return None, Usage()


def confirm_reply(reply_id: int, reminders: ReminderStore, recovery: RecoveryStore, source: InvoiceSource,
                  outbox: Path, api, company: str, today: date, installments: int | None = None) -> list[str]:
    """Applique l'action proposée, confirmée par un humain ; renvoie les erreurs (vide si succès).

    Ordre : 1. tout ce qui peut être refusé est vérifié sans rien écrire ; 2. le statut « appliquée » est posé par une
    transition atomique, seule la confirmation qui l'obtient agit (une double confirmation agit une fois) ;
    3. les effets ; s'ils échouent, ce qu'ils ont créé est défait et la réponse redevient « à traiter ».
    """
    stored = recovery.get_reply(reply_id)
    if stored is None:
        return ["Réponse introuvable."]
    if stored["status"] is not ReplyStatus.TO_REVIEW or stored["client_key"] is None:
        return ["Cette réponse n'est pas à traiter (déjà traitée, classée ou non rattachée)."]
    key, kind = stored["client_key"], stored["proposed_action"]

    prepared = _prepare(kind, stored, key, reminders, recovery, source, outbox, today, installments)
    if isinstance(prepared, str):
        return [prepared]

    if not recovery.set_reply_status(reply_id, ReplyStatus.APPLIED):
        return ["Cette réponse vient d'être traitée."]
    try:
        _apply(kind, stored, key, prepared, reminders, recovery, outbox, api, company, today)
    except Exception as error:
        _undo(recovery, reply_id, prepared, today)
        log.exception("Réponse %d : application échouée", reply_id)
        return [f"Action non appliquée : {error}"]
    reminders.log(None, "reponse_appliquee", {"reponse": reply_id, "client": key, "action": kind.value})
    log.info("Réponse %d appliquée : %s", reply_id, kind.value)
    return []


def _prepare(kind, stored, key, reminders, recovery, source, outbox, today, installments):
    """Vérifie et prépare l'action sans rien écrire ; renvoie un message d'erreur (str) ou les éléments à appliquer."""
    if kind in (ActionKind.POSTPONE, ActionKind.SUSPEND, ActionKind.HUMAN, ActionKind.NONE):
        if kind is ActionKind.POSTPONE and (stored["action_until"] is None or stored["action_until"] <= today):
            return "La date de report est dépassée : à traiter manuellement."
        return {}
    if kind is ActionKind.ACTIVATE_PLAN:
        return _prepare_activation(recovery.open_plan_for(key), reminders, source, outbox, today)
    unpaid = source.unpaid_of(key, today)
    card = reminders.get_client(key)
    if card is None:
        return "Fiche client introuvable."
    if kind is ActionKind.PROPOSE_PLAN:
        if not unpaid:
            return "Le client n'a plus de facture impayée : aucun échéancier à proposer."
        if recovery.open_plan_for(key) is not None:
            return "Un échéancier est déjà proposé ou en cours pour ce client."
        total = sum((i.amount for i in unpaid), Decimal("0"))
        maximum = max_installments(total)
        asked = stored["requested_installments"]
        count = installments if installments is not None else min(asked if asked and asked >= 2 else maximum, maximum)
        try:
            if installments is not None:
                if count < 1:
                    return "Le nombre de mensualités doit être positif."
                base = (total / count).quantize(_CENT, rounding=ROUND_DOWN)
                problems = plan_violations([base] * (count - 1) + [total - base * (count - 1)], total)
                if problems:
                    return f"{count} mensualités refusées pour {total} € : " + " ".join(problems)
            amounts = plan_amounts(total, count)
        except PlanRuleError as error:
            return str(error)
        return {"unpaid": unpaid, "card": card, "total": total, "amounts": amounts, "requested": asked}
    return f"Action inconnue : {kind.value}"


def _prepare_activation(plan: Plan | None, reminders: ReminderStore, source: InvoiceSource, outbox: Path,
                        today: date):
    """Accord du client (réponse confirmée ou saisie à la main) : vérifie sans rien écrire ; message d'erreur (str)
    ou éléments à appliquer. Une facture couverte qui n'est plus impayée bloque l'activation."""
    if plan is None:
        return "Aucun échéancier proposé à activer pour ce client."
    if plan.status is PlanStatus.ACTIVE:  # reprise d'une activation dont le dépôt a échoué, quel que soit le jour
        if plan_eml_path(outbox, plan.id, CONFIRMATION_SUFFIX).exists():
            return "L'échéancier est déjà actif et sa confirmation est déposée."
    elif plan.status is not PlanStatus.PROPOSED:
        return "Aucun échéancier proposé à activer pour ce client."
    card = reminders.get_client(plan.client_key)
    if card is None:
        return "Fiche client introuvable."
    unpaid = source.unpaid_of(plan.client_key, today)
    if plan.status is PlanStatus.PROPOSED:
        still_unpaid = {i.invoice_id for i in unpaid}
        if any(invoice_id not in still_unpaid for invoice_id in plan.invoice_ids):
            return ("Une facture couverte par l'échéancier n'est plus impayée : le montant proposé n'est plus juste. "
                    "Annule l'échéancier et propose-en un nouveau.")
    return {"plan": plan, "card": card, "unpaid": unpaid}


def _activate_and_confirm(prepared: dict, recovery: RecoveryStore, outbox: Path, api, company: str,
                          today: date) -> None:
    """Active l'échéancier (dates fixées depuis `today`) puis prépare et dépose la confirmation au client."""
    plan = prepared["plan"]
    if plan.status is PlanStatus.PROPOSED and not recovery.activate_plan(plan.id, today):
        raise ValueError("L'échéancier n'est plus à l'état proposé.")
    plan = recovery.get_plan(plan.id)
    numbers = _numbers(prepared["unpaid"], plan.invoice_ids)
    draft, *_ = confirmation_draft(api, plan, prepared["card"], company, invoice_numbers=numbers)
    deposit_plan_draft(outbox, plan, prepared["card"], draft, invoice_numbers=numbers)


def record_agreement(plan_id: int, reminders: ReminderStore, recovery: RecoveryStore, source: InvoiceSource,
                     outbox: Path, api, company: str, today: date) -> list[str]:
    """Accord du client saisi à la main (par téléphone, par exemple) : même chemin qu'une réponse « accord »
    confirmée. Renvoie les erreurs (vide si succès)."""
    plan = recovery.get_plan(plan_id)
    prepared = _prepare_activation(plan, reminders, source, outbox, today)
    if isinstance(prepared, str):
        return [prepared]
    try:
        _activate_and_confirm(prepared, recovery, outbox, api, company, today)
    except Exception as error:
        log.exception("Échéancier %d : accord non enregistré", plan_id)
        return [f"Accord non enregistré entièrement : {error}"]
    reminders.log(None, "accord_echeancier_saisi", {"echeancier": plan_id, "client": plan.client_key})
    log.info("Échéancier %d activé sur accord saisi à la main.", plan_id)
    return []


def _numbers(unpaid: list[OverdueInvoice], invoice_ids: list[int]) -> list[str]:
    return [i.number for i in unpaid if i.invoice_id in invoice_ids]


def _apply(kind, stored, key, prepared, reminders, recovery, outbox, api, company, today):
    if kind is ActionKind.POSTPONE:
        recovery.add_hold(key, "report", stored["action_until"],
                          f"Réponse {stored['id']} : report jusqu'au {date_fr(stored['action_until'])}.", today)
    elif kind is ActionKind.SUSPEND:
        recovery.add_hold(key, "suspension", None, f"Réponse {stored['id']} : contestation du client.", today)
    elif kind is ActionKind.HUMAN:
        recovery.add_hold(key, "suspension", None,
                          f"Réponse {stored['id']} : un humain traite la réponse, le client n'est pas relancé.", today)
        log.info("Réponse %d à traiter par un humain, client %s non relancé.", stored["id"], key)
    elif kind is ActionKind.PROPOSE_PLAN:
        unpaid, card = prepared["unpaid"], prepared["card"]
        plan_id = recovery.create_plan(key, [i.invoice_id for i in unpaid], prepared["total"], prepared["amounts"], today)
        prepared["plan_id"] = plan_id
        plan = recovery.get_plan(plan_id)
        numbers = _numbers(unpaid, plan.invoice_ids)
        draft, *_ = proposal_draft(api, plan, card, company, prepared["requested"], invoice_numbers=numbers)
        deposit_plan_draft(outbox, plan, card, draft, invoice_numbers=numbers, requested=prepared["requested"])
    elif kind is ActionKind.ACTIVATE_PLAN:
        _activate_and_confirm(prepared, recovery, outbox, api, company, today)


def _undo(recovery: RecoveryStore, reply_id: int, prepared, today: date) -> None:
    if isinstance(prepared, dict) and prepared.get("plan_id"):
        recovery.set_plan_status(prepared["plan_id"], PlanStatus.CANCELLED, today)
    recovery.reopen_reply(reply_id)


def dismiss_reply(reply_id: int, recovery: RecoveryStore, today: date) -> bool:
    """Classe la réponse sans rien appliquer ; ne touche à aucune pause."""
    done = recovery.set_reply_status(reply_id, ReplyStatus.DISMISSED)
    if done:
        log.info("Réponse %d classée le %s.", reply_id, today.isoformat())
    return done
