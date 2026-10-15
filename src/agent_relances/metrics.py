"""Indicateurs du tableau de bord."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import mean

from agent_relances.models import DraftSource, ReminderStatus
from agent_relances.rules.schedule import InvoiceState
from agent_relances.sources import InvoiceSource
from agent_relances.storage.repository import ReminderStore

STAGE_BUCKETS = ["Pas encore relancée", "Palier 1 fait", "Palier 2 fait", "Palier 3 fait", "Chez un humain"]


@dataclass
class Dashboard:
    overdue_total: Decimal
    overdue_count: int
    by_stage: dict[str, int] = field(default_factory=dict)
    validated: int = 0
    pending: int = 0
    to_humans: int = 0
    recovered_total: Decimal = Decimal("0")
    recovered_count: int = 0
    mean_days_to_payment: float | None = None
    mean_cost_usd: float = 0.0
    fallback_rate: float | None = None


def dashboard(source: InvoiceSource, store: ReminderStore, today: date) -> Dashboard:
    overdue = source.overdue(today)
    states = store.invoice_states()
    by_stage = {bucket: 0 for bucket in STAGE_BUCKETS}
    for invoice in overdue:
        state = states.get(invoice.invoice_id, InvoiceState())
        by_stage[STAGE_BUCKETS[4] if state.escalated else STAGE_BUCKETS[min(state.last_stage, 3)]] += 1

    first = store.first_validated()
    recovered = [(amount, (paid_at - first[invoice_id]).days)
                 for invoice_id, (paid_at, amount) in source.paid_issued().items()
                 if invoice_id in first and first[invoice_id] <= paid_at]

    reminders = store.list_reminders()
    drafted = [r for r in reminders if r.body]  # les passages automatiques à un humain n'ont pas de mail
    count = lambda status: sum(1 for r in reminders if r.status is status)  # noqa: E731
    return Dashboard(
        overdue_total=sum((i.amount for i in overdue), Decimal("0")),
        overdue_count=len(overdue),
        by_stage=by_stage,
        validated=count(ReminderStatus.VALIDATED),
        pending=count(ReminderStatus.TO_VALIDATE),
        to_humans=count(ReminderStatus.HUMAN),
        recovered_total=sum((amount for amount, _ in recovered), Decimal("0")),
        recovered_count=len(recovered),
        mean_days_to_payment=mean(days for _, days in recovered) if recovered else None,
        mean_cost_usd=mean(r.cost_usd for r in drafted) if drafted else 0.0,
        fallback_rate=(sum(1 for r in drafted if r.source is DraftSource.FALLBACK) / len(drafted)) if drafted else None,
    )
