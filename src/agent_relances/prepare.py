"""Orchestration : le code décide, le modèle rédige, le code vérifie, l'humain valide."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import anthropic

from agent_relances.drafting.checker import check_draft
from agent_relances.drafting.fallback import fallback_draft
from agent_relances.drafting.llm import WRITER_MODEL, StepError
from agent_relances.drafting.writer import write_draft
from agent_relances.models import (
    DraftSource, DueReminder, OverdueInvoice, PreparedReminder, ReminderDraft, ReminderStatus, Usage,
)
from agent_relances.outbox import reference, save_reminder_eml, with_reference
from agent_relances.rules.schedule import StageDelays, due_reminders
from agent_relances.sources import InvoiceSource
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore


def prepare_draft(api, due: DueReminder, company: str, model: str = WRITER_MODEL) -> PreparedReminder:
    started = time.perf_counter()
    usage = Usage()
    problems: list[str] = ["Clé d'API absente : modèle de secours utilisé."]
    if api is not None:
        problems = []
        for attempt in range(2):  # les erreurs d'API passagères sont déjà rejouées par le SDK
            try:
                draft, step = write_draft(api, due, company, model, problems)
            except StepError as exc:
                usage += exc.usage
                problems = [f"Erreur du modèle : {exc}"]
                break
            except anthropic.APIError as exc:
                problems = [f"Erreur d'API Claude ({type(exc).__name__})"]
                break
            usage += step
            draft = ReminderDraft(subject=" ".join(draft.subject.split()), body=draft.body)
            problems = check_draft(draft, due)
            if not problems:
                return PreparedReminder(due=due, draft=draft, source=DraftSource.MODEL, first_try_ok=attempt == 0,
                                        usage=usage, seconds=time.perf_counter() - started)
    return PreparedReminder(due=due, draft=fallback_draft(due, company), source=DraftSource.FALLBACK,
                            first_try_ok=False, problems=problems, usage=usage,
                            seconds=time.perf_counter() - started)


@dataclass
class PreparationResult:
    prepared: list[PreparedReminder] = field(default_factory=list)
    missing_clients: list[str] = field(default_factory=list)
    escalated: list[OverdueInvoice] = field(default_factory=list)


def prepare_today(source: InvoiceSource, store: ReminderStore, api, company: str, today: date,
                  model: str = WRITER_MODEL, delays: StageDelays = StageDelays(),
                  on_progress: Callable[[PreparedReminder], None] | None = None,
                  recovery: RecoveryStore | None = None) -> PreparationResult:
    store.discard_pending()
    schedule = due_reminders(source.overdue(today), store.clients_by_key(), source.payment_history(),
                             store.invoice_states(), store.last_contacts(), today, delays,
                             holds=recovery.active_holds(today) if recovery else None,
                             planned_invoice_ids=recovery.planned_invoice_ids() if recovery else None)
    result = PreparationResult(missing_clients=schedule.missing_clients, escalated=schedule.to_humans)
    for invoice in schedule.to_humans:
        reminder_id = store.add_escalation(invoice, today)
        store.log(reminder_id, "passage_humain_auto", {"facture": invoice.number, "retard_jours": invoice.days_late})
    for due in schedule.reminders:
        prepared = prepare_draft(api, due, company, model)
        if on_progress:
            on_progress(prepared)
        reminder_id = store.add_prepared(prepared)
        store.log(reminder_id, "preparee", {"client": due.client.name, "palier": int(due.stage),
                                            "source": prepared.source.value,
                                            "cout_usd": round(prepared.usage.cost_usd, 5)})
        result.prepared.append(prepared)
    return result


@dataclass
class ValidationResult:
    ok: bool
    cancelled: bool = False
    problems: list[str] = field(default_factory=list)


def validate_reminder(store: ReminderStore, source: InvoiceSource, reminder_id: int, subject: str, body: str,
                      outbox: Path, today: date) -> ValidationResult:
    subject = " ".join(subject.split())
    stored = store.get(reminder_id)
    if stored is None or stored.status is not ReminderStatus.TO_VALIDATE:
        return ValidationResult(ok=False, problems=["Relance introuvable ou déjà traitée."])
    unpaid = source.unpaid_ids()
    if any(invoice.invoice_id not in unpaid for invoice in stored.invoices):
        store.cancel(reminder_id, today)
        store.log(reminder_id, "annulee", {"raison": "facture payée entre-temps"})
        return ValidationResult(ok=False, cancelled=True,
                                problems=["Une facture de cette relance a été payée entre-temps : relance annulée."])
    client = store.get_client(stored.client_key)
    if client is None or not client.complete:
        return ValidationResult(ok=False, problems=["Fiche client incomplète : ajoute un email avant de valider."])
    problems = check_draft(ReminderDraft(subject=subject, body=body), stored.to_due(client))
    if problems:
        return ValidationResult(ok=False, problems=problems)
    subject = with_reference(subject, reference("R", reminder_id))
    path = save_reminder_eml(outbox, reminder_id, client, subject, body)
    if not store.validate(reminder_id, subject, body, path, today):
        return ValidationResult(ok=False, problems=["Relance déjà traitée."])
    store.log(reminder_id, "validee", {"modifiee": body != stored.original_body, "fichier": path})
    return ValidationResult(ok=True)
