import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from agent_factures.extraction.models import Direction

from agent_relances.sources import FacturesSourceError, InvoiceSource
from tests.factories import factures_conn, issued

TODAY = date(2026, 10, 15)


def test_overdue_lists_only_unpaid_issued_invoices_past_due():
    conn, repo = factures_conn()
    late = repo.add(issued("AL-1", due=date(2026, 10, 1)), "a.pdf")
    repo.add(issued("AL-2", due=date(2026, 10, 15)), "b.pdf")          # échue aujourd'hui : pas encore en retard
    repo.add(issued("AL-3", due=None), "c.pdf")                         # sans échéance : ignorée
    paid = repo.add(issued("AL-4", due=date(2026, 9, 1)), "d.pdf")
    repo.mark_paid(paid, date(2026, 9, 10))
    repo.add(issued("F-9", direction=Direction.RECEIVED, due=date(2026, 9, 1)), "e.pdf")  # facture reçue
    result = InvoiceSource(conn).overdue(TODAY)
    assert [(i.invoice_id, i.number, i.days_late) for i in result] == [(late, "AL-1", 14)]
    assert result[0].amount == Decimal("1200.50") and result[0].customer == "Hôtel des Canuts"


def test_invoice_without_customer_keeps_an_empty_name():
    conn, repo = factures_conn()
    repo.add(issued("AL-5", customer=None, due=date(2026, 10, 1)), "a.pdf")
    assert InvoiceSource(conn).overdue(TODAY)[0].customer == ""


def test_payment_history_and_paid_issued():
    conn, repo = factures_conn()
    a = repo.add(issued("AL-6", customer="Hôtel des CANUTS", due=date(2026, 9, 1)), "a.pdf")
    repo.mark_paid(a, date(2026, 9, 11))
    unpaid = repo.add(issued("AL-7", due=date(2026, 10, 1)), "b.pdf")
    source = InvoiceSource(conn)
    history = source.payment_history()
    assert list(history) == ["hotel des canuts"]
    assert history["hotel des canuts"][0].days_late == 10
    assert source.paid_issued() == {a: (date(2026, 9, 11), Decimal("1200.50"))}
    assert source.unpaid_ids() == {unpaid}


def test_open_readonly_never_writes(tmp_path):
    path = tmp_path / "factures.db"
    conn, repo = factures_conn(str(path))
    repo.add(issued(), "a.pdf")
    conn.close()
    source = InvoiceSource.open_readonly(path)
    assert len(source.overdue(TODAY)) == 1
    with pytest.raises(sqlite3.OperationalError):
        source._conn.execute("DELETE FROM invoices")


def test_open_readonly_missing_or_invalid_database(tmp_path):
    with pytest.raises(FacturesSourceError, match="introuvable"):
        InvoiceSource.open_readonly(tmp_path / "absente.db")
    bad = tmp_path / "vide.db"
    sqlite3.connect(bad).close()
    with pytest.raises(FacturesSourceError, match="illisible"):
        InvoiceSource.open_readonly(bad)
