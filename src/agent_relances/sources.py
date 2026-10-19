"""Lecture seule des factures émises et des paiements enregistrés par agent-factures."""

import logging
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from agent_factures.extraction.models import Direction
from agent_factures.storage.repository import InvoiceRepository

from agent_relances.models import OverdueInvoice, PaymentRecord, client_key


log = logging.getLogger(__name__)


class FacturesSourceError(RuntimeError):
    pass


class InvoiceSource:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._repo = InvoiceRepository(conn)

    @classmethod
    def open_readonly(cls, path: str | Path) -> "InvoiceSource":
        path = Path(path)
        if not path.is_file():
            raise FacturesSourceError(f"Base d'agent-factures introuvable : {path}")
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT id, data, paid_at FROM invoices LIMIT 1")
        except sqlite3.Error as exc:
            raise FacturesSourceError(f"Base d'agent-factures illisible : {exc}") from exc
        return cls(conn)

    def overdue(self, today: date) -> list[OverdueInvoice]:
        result = []
        for stored in self._repo.list_unpaid(Direction.ISSUED):
            invoice = stored.invoice
            if invoice.due_date is None or invoice.due_date >= today:
                continue
            result.append(OverdueInvoice(
                invoice_id=stored.id, number=invoice.number, customer=(invoice.customer or "").strip(),
                amount=invoice.amount_incl_tax, issue_date=invoice.issue_date, due_date=invoice.due_date,
                days_late=(today - invoice.due_date).days,
            ))
        return result

    def unpaid_of(self, client_key_: str, today: date) -> list[OverdueInvoice]:
        """Toutes les factures impayées d'un client, échues ou non (`days_late` ≤ 0 si pas encore échue)."""
        wanted = client_key(client_key_)
        result = []
        for stored in self._repo.list_unpaid(Direction.ISSUED):
            invoice = stored.invoice
            if client_key(invoice.customer or "") != wanted:
                continue
            due = invoice.due_date or invoice.issue_date  # sans échéance, la facture est due dès son émission
            if due is None:
                log.warning("Facture impayée %s sans échéance ni date d'émission : ignorée.", invoice.number)
                continue
            result.append(OverdueInvoice(
                invoice_id=stored.id, number=invoice.number, customer=(invoice.customer or "").strip(),
                amount=invoice.amount_incl_tax, issue_date=invoice.issue_date, due_date=due,
                days_late=(today - due).days,
            ))
        return result

    def files_of(self, invoice_ids: list[int]) -> dict[int, str]:
        """Fichier d'origine (`source_file` d'agent-factures) de chaque facture demandée qui existe."""
        result = {}
        for invoice_id in invoice_ids:
            stored = self._repo.get(invoice_id)
            if stored is not None:
                result[invoice_id] = stored.source_file
        return result

    def numbers_of(self, invoice_ids: list[int]) -> dict[int, str]:
        """Numéro de chaque facture demandée qui existe."""
        result = {}
        for invoice_id in invoice_ids:
            stored = self._repo.get(invoice_id)
            if stored is not None:
                result[invoice_id] = stored.invoice.number
        return result

    def unpaid_ids(self) -> set[int]:
        return {stored.id for stored in self._repo.list_unpaid(Direction.ISSUED)}

    def _paid_issued_invoices(self):
        for stored in self._repo.list_paid():
            if stored.invoice.direction is Direction.ISSUED and stored.paid_at is not None:
                yield stored

    def payment_history(self) -> dict[str, list[PaymentRecord]]:
        history: dict[str, list[PaymentRecord]] = {}
        for stored in self._paid_issued_invoices():
            invoice = stored.invoice
            if invoice.due_date is None or not invoice.customer:
                continue
            history.setdefault(client_key(invoice.customer), []).append(
                PaymentRecord(customer=invoice.customer, due_date=invoice.due_date, paid_at=stored.paid_at)
            )
        return history

    def paid_issued(self) -> dict[int, tuple[date, Decimal]]:
        return {stored.id: (stored.paid_at, stored.invoice.amount_incl_tax) for stored in self._paid_issued_invoices()}
