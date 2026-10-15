from datetime import date, timedelta
from decimal import Decimal

import pytest

from agent_relances.models import (
    DraftSource, PreparedReminder, ReminderDraft, ReminderStatus, Stage, Tone, Usage,
)
from agent_relances.rules.schedule import InvoiceState
from agent_relances.storage.db import connect
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, due_reminder, overdue

TODAY = date(2026, 10, 15)


@pytest.fixture
def store():
    return ReminderStore(connect())


def prepared(invoices=None, stage=Stage.COURTEOUS):
    due = due_reminder(invoices=invoices, stage=stage)
    return PreparedReminder(due=due, draft=ReminderDraft(subject="Rappel", body="Bonjour"), source=DraftSource.MODEL,
                            first_try_ok=True, usage=Usage(cost_usd=0.004), seconds=2.5)


def test_client_cards(store):
    store.save_client(client())
    store.save_client(client(email="nouveau@hotel-canuts.fr"))
    assert [c.email for c in store.list_clients()] == ["nouveau@hotel-canuts.fr"]
    assert store.get_client("hotel des canuts").contact == "Paul Girard"
    assert list(store.clients_by_key()) == ["hotel des canuts"]


def test_prepared_reminder_roundtrip(store):
    invoices = [overdue(1, "AL-1", amount="1200.50"), overdue(2, "AL-2", amount="99.50")]
    reminder_id = store.add_prepared(prepared(invoices))
    stored = store.get(reminder_id)
    assert stored.status is ReminderStatus.TO_VALIDATE and stored.prepared_on == TODAY
    assert [i.number for i in stored.invoices] == ["AL-1", "AL-2"] and stored.total == Decimal("1300.00")
    assert stored.invoice_stages == {1: 1, 2: 1} and stored.original_body == "Bonjour"
    due = stored.to_due(client())
    assert due.stage is Stage.COURTEOUS and due.tone is Tone.LIGHT and due.total == Decimal("1300.00")


def test_decisions_apply_only_once(store):
    reminder_id = store.add_prepared(prepared())
    assert store.validate(reminder_id, "Objet", "Corps modifié", "/tmp/r.eml", TODAY)
    assert not store.validate(reminder_id, "Autre", "Autre", "/tmp/x.eml", TODAY)
    assert not store.to_human(reminder_id, TODAY)
    stored = store.get(reminder_id)
    assert (stored.status, stored.body, stored.eml_path, stored.decided_on) == (
        ReminderStatus.VALIDATED, "Corps modifié", "/tmp/r.eml", TODAY)


def test_discard_pending_keeps_decisions(store):
    kept = store.add_prepared(prepared())
    store.validate(kept, "O", "C", "/tmp/r.eml", TODAY)
    store.add_prepared(prepared())
    assert store.discard_pending() == 1
    assert [r.id for r in store.list_reminders()] == [kept]


def test_invoice_states_and_last_contacts(store):
    first = store.add_prepared(prepared([overdue(1, "AL-1")]))
    store.validate(first, "O", "C", "/tmp/1.eml", TODAY - timedelta(days=20))
    second = store.add_prepared(prepared([overdue(1, "AL-1")], stage=Stage.FIRM))
    store.validate(second, "O", "C", "/tmp/2.eml", TODAY - timedelta(days=3))
    later = store.add_prepared(prepared([overdue(2, "AL-2")]))
    store.postpone(later, 10, TODAY)
    escalated = store.add_escalation(overdue(3, "AL-3"), TODAY)
    states = store.invoice_states()
    assert states[1] == InvoiceState(last_stage=2)
    assert states[2] == InvoiceState(postponed_until=TODAY + timedelta(days=10))
    assert states[3].escalated and store.get(escalated).status is ReminderStatus.HUMAN
    assert store.last_contacts() == {"hotel des canuts": TODAY - timedelta(days=3)}
    assert store.first_validated() == {1: TODAY - timedelta(days=20)}


def test_add_validated_history(store):
    due = due_reminder()
    reminder_id = store.add_validated(due, ReminderDraft(subject="S", body="B"), TODAY - timedelta(days=8))
    stored = store.get(reminder_id)
    assert stored.status is ReminderStatus.VALIDATED and stored.decided_on == TODAY - timedelta(days=8)
    assert store.invoice_states()[1].last_stage == 1


def test_log(store):
    store.log(1, "validee", {"fichier": "r.eml"})
    assert store.list_log()[0]["detail"] == {"fichier": "r.eml"}
