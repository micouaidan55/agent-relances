"""Quelles relances préparer aujourd'hui : paliers, regroupement par client, délai minimal (spec §3.3 à §3.7).

Fonctions pures : aucune lecture de base, aucun appel au modèle.
"""

from dataclasses import dataclass, field
from datetime import date

from agent_relances.models import Client, DueReminder, OverdueInvoice, PaymentRecord, Stage, client_key
from agent_relances.rules.legal import needs_legal_notice
from agent_relances.rules.profile import client_profile
from agent_relances.rules.tone import target_tone

MIN_GAP_DAYS = 7
ESCALATE = "humain"
UNNAMED_CLIENT = "(client non renseigné)"


@dataclass(frozen=True)
class StageDelays:
    courteous: int = 3
    firm: int = 15
    final: int = 30

    def for_stage(self, stage: Stage) -> int:
        return {Stage.COURTEOUS: self.courteous, Stage.FIRM: self.firm, Stage.FINAL: self.final}[Stage(stage)]


@dataclass(frozen=True)
class InvoiceState:
    last_stage: int = 0
    escalated: bool = False
    postponed_until: date | None = None


@dataclass
class Schedule:
    reminders: list[DueReminder] = field(default_factory=list)
    missing_clients: list[str] = field(default_factory=list)
    to_humans: list[OverdueInvoice] = field(default_factory=list)


def next_step(invoice: OverdueInvoice, state: InvoiceState, delays: StageDelays, today: date) -> Stage | str | None:
    """Palier dû aujourd'hui pour une facture, ESCALATE après le palier 3, ou None."""
    if state.escalated:
        return None
    if state.postponed_until is not None and today < state.postponed_until:
        return None
    if state.last_stage >= Stage.FINAL:
        return ESCALATE
    stage = Stage(state.last_stage + 1)
    return stage if invoice.days_late >= delays.for_stage(stage) else None


def due_reminders(
    overdue: list[OverdueInvoice],
    clients: dict[str, Client],
    history: dict[str, list[PaymentRecord]],
    states: dict[int, InvoiceState],
    last_contacts: dict[str, date],
    today: date,
    delays: StageDelays = StageDelays(),
) -> Schedule:
    schedule = Schedule()
    due_by_client: dict[str, list[tuple[OverdueInvoice, Stage]]] = {}
    names: dict[str, str] = {}
    overdue_count: dict[str, int] = {}
    for invoice in overdue:
        key = client_key(invoice.customer)
        overdue_count[key] = overdue_count.get(key, 0) + 1
        step = next_step(invoice, states.get(invoice.invoice_id, InvoiceState()), delays, today)
        if step is None:
            continue
        if step == ESCALATE:
            schedule.to_humans.append(invoice)
            continue
        names.setdefault(key, invoice.customer or UNNAMED_CLIENT)
        due_by_client.setdefault(key, []).append((invoice, step))

    missing = set()
    for key, items in due_by_client.items():
        client = clients.get(key) if key else None
        if client is None or not client.complete:
            missing.add(names[key])
            continue
        last = last_contacts.get(key)
        if last is not None and (today - last).days < MIN_GAP_DAYS:
            continue
        stage = max(step for _, step in items)
        profile = client_profile(history.get(key, []), overdue_count[key], today)
        items.sort(key=lambda item: (item[0].due_date, item[0].number))
        schedule.reminders.append(DueReminder(
            client=client, profile=profile, stage=stage, tone=target_tone(stage, profile),
            invoices=[invoice for invoice, _ in items],
            invoice_stages={invoice.invoice_id: int(step) for invoice, step in items},
            legal_notice=needs_legal_notice(stage, client.professional), today=today,
        ))
    schedule.reminders.sort(key=lambda reminder: reminder.total, reverse=True)
    schedule.missing_clients = sorted(missing)
    return schedule
