from datetime import date, timedelta

import anthropic
import httpx

from agent_relances.models import DraftSource, ReminderDraft, ReminderStatus, Stage, Tone
from agent_relances.prepare import prepare_draft, prepare_today, validate_reminder
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, due_reminder, factures_conn, issued, overdue
from tests.fakes import FakeClient, reply

TODAY = date(2026, 10, 15)
COMPANY = "Atelier Lumière SAS"
GOOD = ReminderDraft(subject="Rappel facture AL-2026-101",
                     body="Bonjour Paul, la facture AL-2026-101 de 1 200,50 € TTC, échue le 01/10/2026, reste à régler. "
                          "Pouvez-vous nous indiquer une date de règlement ? Atelier Lumière SAS")
WRONG = ReminderDraft(subject="Rappel", body=GOOD.body.replace("1 200,50 €", "1 250,00 €"))


def test_first_try_success():
    result = prepare_draft(FakeClient([reply(GOOD)]), due_reminder(), COMPANY)
    assert (result.source, result.first_try_ok, result.problems) == (DraftSource.MODEL, True, [])
    assert result.usage.cost_usd > 0


def test_second_try_after_a_wrong_fact():
    api = FakeClient([reply(WRONG), reply(GOOD)])
    result = prepare_draft(api, due_reminder(), COMPANY)
    assert (result.source, result.first_try_ok) == (DraftSource.MODEL, False)
    assert "Montant absent : 1 200,50 €" in api.messages.calls[1]["messages"][0]["content"]


def test_fallback_after_two_wrong_drafts():
    result = prepare_draft(FakeClient([reply(WRONG), reply(WRONG)]), due_reminder(), COMPANY)
    assert result.source is DraftSource.FALLBACK and result.problems
    assert "AL-2026-101" in result.draft.body


def test_fallback_without_api_or_on_api_error():
    assert prepare_draft(None, due_reminder(), COMPANY).source is DraftSource.FALLBACK
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    failing = FakeClient([anthropic.APIConnectionError(request=request)])
    assert prepare_draft(failing, due_reminder(), COMPANY).source is DraftSource.FALLBACK


def setup(tmp_path, due=date(2026, 10, 1)):
    conn, repo = factures_conn()
    invoice_id = repo.add(issued("AL-2026-101", due=due), "a.pdf")
    store = ReminderStore(connect())
    store.save_client(client())
    return InvoiceSource(conn), store, repo, invoice_id


def test_prepare_today_end_to_end_and_no_duplicates(tmp_path):
    source, store, _, _ = setup(tmp_path)
    first = prepare_today(source, store, FakeClient([reply(GOOD)] * 2), COMPANY, TODAY)
    assert len(first.prepared) == 1 and first.missing_clients == [] and first.escalated == []
    prepare_today(source, store, FakeClient([reply(GOOD)] * 2), COMPANY, TODAY)       # double clic
    assert len(store.list_reminders({ReminderStatus.TO_VALIDATE})) == 1


def test_validate_then_next_day_respects_the_minimum_gap(tmp_path):
    source, store, _, _ = setup(tmp_path)
    prepare_today(source, store, FakeClient([reply(GOOD)]), COMPANY, TODAY)
    [pending] = store.list_reminders({ReminderStatus.TO_VALIDATE})
    outbox = tmp_path / "deposees"
    result = validate_reminder(store, source, pending.id, GOOD.subject, GOOD.body, outbox, TODAY)
    assert result.ok and list(outbox.glob("*.eml"))
    again = validate_reminder(store, source, pending.id, GOOD.subject, GOOD.body, outbox, TODAY)   # double validation
    assert not again.ok and len(list(outbox.glob("*.eml"))) == 1
    later = prepare_today(source, store, FakeClient([reply(GOOD)]), COMPANY, TODAY + timedelta(days=3))
    assert later.prepared == []


def test_edited_draft_with_a_wrong_amount_is_refused(tmp_path):
    source, store, _, _ = setup(tmp_path)
    prepare_today(source, store, None, COMPANY, TODAY)
    [pending] = store.list_reminders({ReminderStatus.TO_VALIDATE})
    result = validate_reminder(store, source, pending.id, WRONG.subject, WRONG.body, tmp_path, TODAY)
    assert not result.ok and "Montant absent : 1 200,50 €" in result.problems
    assert store.get(pending.id).status is ReminderStatus.TO_VALIDATE


def test_invoice_paid_in_between_cancels_the_reminder(tmp_path):
    source, store, repo, invoice_id = setup(tmp_path)
    prepare_today(source, store, None, COMPANY, TODAY)
    [pending] = store.list_reminders({ReminderStatus.TO_VALIDATE})
    repo.mark_paid(invoice_id, TODAY)
    result = validate_reminder(store, source, pending.id, pending.subject, pending.body, tmp_path, TODAY)
    assert result.cancelled and store.get(pending.id).status is ReminderStatus.CANCELLED


def test_escalation_and_missing_cards_are_reported(tmp_path):
    conn, repo = factures_conn()
    repo.add(issued("AL-9", customer="Inconnu SARL", due=date(2026, 10, 1)), "a.pdf")
    late = repo.add(issued("AL-8", due=date(2026, 8, 1)), "b.pdf")
    store = ReminderStore(connect())
    store.save_client(client())
    final = due_reminder(invoices=[overdue(late, "AL-8", due=date(2026, 8, 1))], stage=Stage.FINAL, tone=Tone.FIRM,
                         legal=True)
    store.add_validated(final, ReminderDraft(subject="S", body="B"), TODAY - timedelta(days=20))
    result = prepare_today(InvoiceSource(conn), store, None, COMPANY, TODAY)
    assert result.missing_clients == ["Inconnu SARL"]
    assert [i.number for i in result.escalated] == ["AL-8"]
    assert store.list_reminders({ReminderStatus.HUMAN})
