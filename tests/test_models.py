from datetime import date
from decimal import Decimal

from agent_relances.formatting import date_fr, euros
from agent_relances.models import (
    Client, DueReminder, OverdueInvoice, PaymentRecord, Profile, Stage, Tone, Usage, client_key,
)


def invoice(number="AL-1", amount="100.00", days_late=5):
    return OverdueInvoice(invoice_id=1, number=number, customer="Hôtel des Canuts", amount=Decimal(amount),
                          issue_date=date(2026, 9, 1), due_date=date(2026, 10, 1), days_late=days_late)


def test_client_key_ignores_case_accents_and_spaces():
    assert client_key("  Hôtel des   CANUTS ") == "hotel des canuts"
    assert Client(name="Hôtel des Canuts").key == client_key("hotel des canuts")


def test_client_is_complete_only_with_an_email():
    assert not Client(name="A").complete
    assert not Client(name="A", email="pas-une-adresse").complete
    assert Client(name="A", email="compta@a.fr").complete


def test_due_reminder_total_and_payment_lateness():
    due = DueReminder(client=Client(name="A", email="a@a.fr"), profile=Profile.GOOD, stage=Stage.COURTEOUS,
                      tone=Tone.LIGHT, invoices=[invoice("AL-1", "1200.50"), invoice("AL-2", "99.50")],
                      invoice_stages={1: 1}, legal_notice=False, today=date(2026, 10, 15))
    assert due.total == Decimal("1300.00")
    assert PaymentRecord(customer="A", due_date=date(2026, 9, 1), paid_at=date(2026, 9, 11)).days_late == 10
    assert PaymentRecord(customer="A", due_date=date(2026, 9, 1), paid_at=date(2026, 8, 25)).days_late == 0


def test_usage_adds_up():
    total = Usage(input_tokens=10, output_tokens=2, cost_usd=0.1) + Usage(input_tokens=5, output_tokens=1, cost_usd=0.2)
    assert (total.input_tokens, total.output_tokens) == (15, 3)
    assert abs(total.cost_usd - 0.3) < 1e-9


def test_french_formats():
    assert euros(Decimal("1200.5")) == "1 200,50 €"
    assert date_fr(date(2026, 10, 1)) == "01/10/2026"
