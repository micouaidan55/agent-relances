"""Fabriques de données de test."""

from datetime import date
from decimal import Decimal

from agent_factures.extraction.models import Direction, DocumentType, Invoice
from agent_factures.storage.db import connect
from agent_factures.storage.repository import InvoiceRepository

from agent_relances.models import Client, DueReminder, OverdueInvoice, Profile, Stage, Tone


def issued(number="AL-2026-101", customer="Hôtel des Canuts", amount="1200.50", issue=date(2026, 9, 1),
           due: date | None = date(2026, 10, 1), direction=Direction.ISSUED) -> Invoice:
    ttc = Decimal(amount)
    ht = (ttc / Decimal("1.2")).quantize(Decimal("0.01"))
    return Invoice(doc_type=DocumentType.INVOICE, direction=direction, supplier="Atelier Lumière SAS",
                   customer=customer, number=number, issue_date=issue, due_date=due,
                   amount_excl_tax=ht, vat_amount=ttc - ht, amount_incl_tax=ttc)


def factures_conn(path: str = ":memory:"):
    conn = connect(path)
    return conn, InvoiceRepository(conn)


def overdue(invoice_id=1, number="AL-2026-101", customer="Hôtel des Canuts", amount="1200.50",
            due=date(2026, 10, 1), today=date(2026, 10, 15)) -> OverdueInvoice:
    return OverdueInvoice(invoice_id=invoice_id, number=number, customer=customer, amount=Decimal(amount),
                          issue_date=date(2026, 9, 1), due_date=due, days_late=(today - due).days)


def client(name="Hôtel des Canuts", email="compta@hotel-canuts.fr", contact="Paul Girard", professional=True) -> Client:
    return Client(name=name, email=email, contact=contact, professional=professional)


def due_reminder(invoices=None, stage=Stage.COURTEOUS, tone=Tone.LIGHT, legal=False, professional=True,
                 profile=Profile.GOOD, today=date(2026, 10, 15)) -> DueReminder:
    invoices = invoices or [overdue()]
    return DueReminder(client=client(professional=professional), profile=profile, stage=stage, tone=tone,
                       invoices=invoices, invoice_stages={i.invoice_id: int(stage) for i in invoices},
                       legal_notice=legal, today=today)
