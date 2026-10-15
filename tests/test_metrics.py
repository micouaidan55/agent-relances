from datetime import date, timedelta
from decimal import Decimal

from agent_relances.metrics import dashboard
from agent_relances.models import DraftSource, PreparedReminder, ReminderDraft, Usage
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.repository import ReminderStore
from tests.factories import due_reminder, factures_conn, issued, overdue

TODAY = date(2026, 10, 15)


def test_dashboard():
    conn, repo = factures_conn()
    late = repo.add(issued("AL-1", amount="1000.00", due=date(2026, 10, 1)), "a.pdf")
    recovered = repo.add(issued("AL-2", amount="500.00", due=date(2026, 9, 1)), "b.pdf")
    repo.add(issued("AL-3", amount="250.00", due=date(2026, 10, 10)), "c.pdf")
    store = ReminderStore(connect())
    store.add_validated(due_reminder([overdue(recovered, "AL-2", amount="500.00", due=date(2026, 9, 1))]),
                        ReminderDraft(subject="S", body="B"), date(2026, 9, 10))
    repo.mark_paid(recovered, date(2026, 9, 14))
    store.add_validated(due_reminder([overdue(late, "AL-1", amount="1000.00")]), ReminderDraft(subject="S", body="B"),
                        TODAY - timedelta(days=2))
    store.add_prepared(PreparedReminder(due=due_reminder(), draft=ReminderDraft(subject="S", body="B"),
                                        source=DraftSource.FALLBACK, first_try_ok=False, usage=Usage(cost_usd=0.002)))
    board = dashboard(InvoiceSource(conn), store, TODAY)
    assert (board.overdue_total, board.overdue_count) == (Decimal("1250.00"), 2)
    assert board.by_stage["Pas encore relancée"] == 1 and board.by_stage["Palier 1 fait"] == 1
    assert (board.recovered_total, board.recovered_count, board.mean_days_to_payment) == (Decimal("500.00"), 1, 4.0)
    assert (board.validated, board.pending) == (2, 1)
    assert abs(board.fallback_rate - 1 / 3) < 1e-9
