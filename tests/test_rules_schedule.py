from datetime import date, timedelta

import pytest

from agent_relances.models import Profile, Stage, Tone
from agent_relances.rules.schedule import (
    ESCALATE, InvoiceState, StageDelays, due_reminders, next_step,
)
from tests.factories import client, overdue

TODAY = date(2026, 10, 15)
DELAYS = StageDelays()


def late(days, invoice_id=1, number="AL-1", customer="Hôtel des Canuts", amount="100.00"):
    return overdue(invoice_id=invoice_id, number=number, customer=customer, amount=amount,
                   due=TODAY - timedelta(days=days), today=TODAY)


@pytest.mark.parametrize("days,state,expected", [
    (2, InvoiceState(), None),
    (3, InvoiceState(), Stage.COURTEOUS),
    (40, InvoiceState(), Stage.COURTEOUS),                  # on ne saute jamais un palier
    (14, InvoiceState(last_stage=1), None),
    (15, InvoiceState(last_stage=1), Stage.FIRM),
    (30, InvoiceState(last_stage=2), Stage.FINAL),
    (60, InvoiceState(last_stage=3), ESCALATE),
    (60, InvoiceState(last_stage=1, escalated=True), None),
    (20, InvoiceState(postponed_until=TODAY + timedelta(days=1)), None),
    (20, InvoiceState(postponed_until=TODAY), Stage.COURTEOUS),
])
def test_next_step(days, state, expected):
    assert next_step(late(days), state, DELAYS, TODAY) == expected


def run(invoices, clients=None, states=None, last_contacts=None, history=None):
    clients = clients if clients is not None else [client()]
    return due_reminders(invoices, {c.key: c for c in clients}, history or {}, states or {}, last_contacts or {}, TODAY)


def test_one_reminder_with_profile_tone_and_no_legal_notice():
    [reminder] = run([late(5)]).reminders
    assert (reminder.stage, reminder.profile, reminder.tone, reminder.legal_notice) == (
        Stage.COURTEOUS, Profile.OCCASIONAL, Tone.LIGHT, False)
    assert reminder.invoice_stages == {1: 1}


def test_invoices_of_one_client_are_grouped_at_the_most_advanced_stage():
    invoices = [late(20, 1, "AL-1", "Hôtel des Canuts"), late(16, 2, "AL-2", "HOTEL  des canuts")]
    states = {1: InvoiceState(last_stage=1)}
    [reminder] = run(invoices, states=states).reminders
    assert reminder.stage is Stage.FIRM
    assert [i.number for i in reminder.invoices] == ["AL-1", "AL-2"]
    assert reminder.invoice_stages == {1: 2, 2: 1}
    assert reminder.profile is Profile.CHRONIC          # deux factures en retard en même temps


def test_minimum_gap_between_two_reminders_to_the_same_client():
    key = client().key
    assert run([late(20)], last_contacts={key: TODAY - timedelta(days=6)}).reminders == []
    assert len(run([late(20)], last_contacts={key: TODAY - timedelta(days=7)}).reminders) == 1


def test_missing_or_incomplete_client_cards():
    result = run([late(5, 1, "AL-1", "Hôtel des Canuts SAS"), late(5, 2, "AL-2", ""),
                  late(5, 3, "AL-3", "Galerie Nova")],
                 clients=[client(), client(name="Galerie Nova", email="")])
    assert result.reminders == []
    assert result.missing_clients == ["(client non renseigné)", "Galerie Nova", "Hôtel des Canuts SAS"]


def test_last_stage_done_goes_to_a_human():
    result = run([late(60)], states={1: InvoiceState(last_stage=3)})
    assert result.reminders == [] and [i.number for i in result.to_humans] == ["AL-1"]


def test_legal_notice_depends_on_the_client_type():
    states = {1: InvoiceState(last_stage=2)}
    [pro] = run([late(31)], states=states).reminders
    [private] = run([late(31)], clients=[client(professional=False)], states=states).reminders
    assert (pro.stage, pro.legal_notice, private.legal_notice) == (Stage.FINAL, True, False)


def test_reminders_are_sorted_by_amount_due():
    invoices = [late(5, 1, "AL-1", "Hôtel des Canuts", "100.00"), late(5, 2, "AL-2", "Galerie Nova", "900.00")]
    result = run(invoices, clients=[client(), client(name="Galerie Nova", email="a@nova.fr")])
    assert [r.client.name for r in result.reminders] == ["Galerie Nova", "Hôtel des Canuts"]
