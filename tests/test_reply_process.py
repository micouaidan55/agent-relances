from datetime import date
from decimal import Decimal
from email.message import EmailMessage

import pytest

from agent_relances.models import ActionKind, PlanStatus, ReplyIntent, ReplyReading, ReplyStatus
from agent_relances.replies.process import confirm_reply, dismiss_reply, process_replies
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, factures_conn, issued
from tests.fakes import FakeClient, reply

TODAY = date(2026, 10, 16)
KEY = "hotel des canuts"
D = Decimal


def eml(message_id="<m1@x>", sender="compta@hotel-canuts.fr", subject="Re: Relance", body="Bonjour") -> bytes:
    message = EmailMessage()
    message["From"] = f"Compta <{sender}>"
    message["To"] = "relances@atelier.fr"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Thu, 15 Oct 2026 09:30:00 +0200"
    message.set_content(body)
    return message.as_bytes()


def reading(intent, promised=None, installments=None) -> ReplyReading:
    return ReplyReading(intent=intent, promised_date=promised, requested_installments=installments, summary="Résumé.")


class World:
    def __init__(self, tmp_path, amounts=("100.00", "150.00", "200.00")):
        self.conn, self.repo = factures_conn()
        self.ids = [self.repo.add(issued(f"AL-{i}", amount=a, due=date(2026, 9, 1 + i)), f"{i}.pdf")
                    for i, a in enumerate(amounts)]
        self.source = InvoiceSource(self.conn)
        db = connect()
        self.reminders, self.recovery = ReminderStore(db), RecoveryStore(db)
        self.reminders.save_client(client())
        self.inbox, self.outbox = tmp_path / "inbox", tmp_path / "outbox"
        self.inbox.mkdir()

    def receive(self, parsed, **kw) -> int:
        (self.inbox / f"{len(list(self.inbox.iterdir()))}.eml").write_bytes(eml(**kw))
        ids = process_replies(self.inbox, self.reminders, self.recovery, self.source, FakeClient([reply(parsed)]),
                              "Atelier Lumière", "atelier.fr", TODAY)
        assert len(ids) == 1
        return ids[0]

    def confirm(self, reply_id, **kw) -> list[str]:
        return confirm_reply(reply_id, self.reminders, self.recovery, self.source, self.outbox, None,
                             "Atelier Lumière", TODAY, **kw)

    def status(self, reply_id) -> ReplyStatus:
        return self.recovery.get_reply(reply_id)["status"]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_unpaid_of_includes_not_yet_due_and_normalizes_names():
    conn, repo = factures_conn()
    late = repo.add(issued("AL-1", customer="HOTEL des Canuts", due=date(2026, 10, 1)), "a.pdf")
    soon = repo.add(issued("AL-2", due=date(2026, 11, 1)), "b.pdf")
    paid = repo.add(issued("AL-3", due=date(2026, 9, 1)), "c.pdf")
    repo.mark_paid(paid, date(2026, 9, 5))
    repo.add(issued("AL-4", customer="Café Rond", due=date(2026, 9, 1)), "d.pdf")
    result = InvoiceSource(conn).unpaid_of(KEY, TODAY)
    assert sorted(i.invoice_id for i in result) == sorted([late, soon])
    assert {i.invoice_id: i.days_late for i in result}[soon] < 0


def test_promised_date_postpones_until_date_plus_three_days_after_confirmation(world):
    rid = world.receive(reading(ReplyIntent.PROMISED_DATE, "2026-10-30"), body="Nous paierons le 30.")
    stored = world.recovery.get_reply(rid)
    assert stored["status"] is ReplyStatus.TO_REVIEW and stored["proposed_action"] is ActionKind.POSTPONE
    assert stored["cost_usd"] > 0
    assert world.recovery.active_holds(TODAY) == {}          # rien avant la confirmation
    assert world.confirm(rid) == []
    assert world.recovery.active_holds(TODAY) == {KEY: date(2026, 11, 2)}
    assert world.status(rid) is ReplyStatus.APPLIED


def test_dispute_suspends_without_end(world):
    rid = world.receive(reading(ReplyIntent.DISPUTE), body="Je conteste.")
    assert world.confirm(rid) == []
    assert world.recovery.active_holds(TODAY) == {KEY: None}


def test_plan_request_proposes_four_installments_covering_all_unpaid(world):
    rid = world.receive(reading(ReplyIntent.PLAN_REQUEST, installments=6), body="En 6 fois ?")
    assert world.confirm(rid) == []
    plan = world.recovery.open_plan_for(KEY)
    assert plan.status is PlanStatus.PROPOSED and plan.total == D("450.00")
    assert [i.amount for i in plan.installments] == [D("112.50")] * 4
    assert sorted(plan.invoice_ids) == sorted(world.ids)
    emls = list(world.outbox.glob("*.eml"))
    assert len(emls) == 1 and f"E-{plan.id:05d}" in emls[0].read_text(errors="ignore")


def test_agreement_activates_the_proposed_plan_with_dates(world):
    world.confirm(world.receive(reading(ReplyIntent.PLAN_REQUEST), message_id="<a@x>"))
    rid = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<b@x>", body="D'accord.")
    assert world.recovery.get_reply(rid)["proposed_action"] is ActionKind.ACTIVATE_PLAN
    assert world.confirm(rid) == []
    plan = world.recovery.open_plan_for(KEY)
    assert plan.status is PlanStatus.ACTIVE and plan.agreed_on == TODAY
    assert plan.installments[0].due_date == date(2026, 11, 15)
    confirmation, = world.outbox.glob("*.eml")           # même fichier par échéancier : la confirmation remplace la proposition
    assert "15/11/2026" in confirmation.read_text(errors="ignore")


def test_human_override_outside_the_rule_is_refused_and_nothing_is_created(world):
    rid = world.receive(reading(ReplyIntent.PLAN_REQUEST))
    errors = world.confirm(rid, installments=5)
    assert errors and "5" in errors[0]
    assert world.recovery.open_plan_for(KEY) is None
    assert world.status(rid) is ReplyStatus.TO_REVIEW
    assert not world.outbox.exists() or not list(world.outbox.glob("*.eml"))
    assert world.confirm(rid, installments=3) == []     # dans la règle : 3 x 150 €
    assert [i.amount for i in world.recovery.open_plan_for(KEY).installments] == [D("150.00")] * 3


def test_override_three_installments_for_250_is_refused(tmp_path):
    world = World(tmp_path, amounts=("100.00", "150.00"))
    rid = world.receive(reading(ReplyIntent.PLAN_REQUEST))
    assert world.confirm(rid, installments=3)
    assert world.recovery.open_plan_for(KEY) is None


def test_paid_but_nothing_unpaid_is_archived_without_hold(tmp_path):
    world = World(tmp_path)
    for invoice_id in world.ids:
        world.repo.mark_paid(invoice_id, date(2026, 10, 10))
    rid = world.receive(reading(ReplyIntent.PAYMENT_ANNOUNCED), body="J'ai payé.")
    assert world.recovery.get_reply(rid)["proposed_action"] is ActionKind.NONE
    assert world.confirm(rid) == []
    assert world.recovery.active_holds(TODAY) == {}
    assert world.recovery.open_plan_for(KEY) is None
    assert world.status(rid) is ReplyStatus.APPLIED


def test_agreement_without_proposal_goes_to_human_suspension(world):
    rid = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), body="D'accord.")
    assert world.recovery.get_reply(rid)["proposed_action"] is ActionKind.HUMAN
    assert world.confirm(rid) == []
    assert world.recovery.open_plan_for(KEY) is None
    assert world.recovery.active_holds(TODAY) == {KEY: None}


def test_confirming_twice_acts_once(world):
    rid = world.receive(reading(ReplyIntent.PLAN_REQUEST))
    assert world.confirm(rid) == []
    assert world.confirm(rid)                                   # message d'erreur, pas d'action
    assert len(world.recovery.list_plans()) == 1
    assert len(list(world.outbox.glob("*.eml"))) == 1


def test_confirming_twice_postpone_creates_one_hold(world):
    rid = world.receive(reading(ReplyIntent.PAYMENT_ANNOUNCED))
    world.confirm(rid)
    world.confirm(rid)
    assert world.recovery.active_holds(TODAY) == {KEY: date(2026, 10, 23)}
    assert world.recovery._conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 1


def test_failed_side_effect_restores_status_and_cancels_plan(world, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disque plein")
    monkeypatch.setattr("agent_relances.replies.process.deposit_plan_draft", boom)
    rid = world.receive(reading(ReplyIntent.PLAN_REQUEST))
    errors = world.confirm(rid)
    assert errors and "disque plein" in errors[0]
    assert world.status(rid) is ReplyStatus.TO_REVIEW
    assert world.recovery.open_plan_for(KEY) is None


def test_unmatched_reply_is_kept_without_model_call(world):
    (world.inbox / "x.eml").write_bytes(eml(sender="x@inconnu.io", subject="Re: [Réf. R-99999]"))
    api = FakeClient([])
    ids = process_replies(world.inbox, world.reminders, world.recovery, world.source, api,
                          "Atelier Lumière", "atelier.fr", TODAY)
    stored = world.recovery.get_reply(ids[0])
    assert stored["status"] is ReplyStatus.UNMATCHED and stored["client_key"] is None
    assert api.messages.calls == []
    assert world.confirm(ids[0])                                # jamais applicable


def test_reader_failure_falls_back_to_human(world):
    (world.inbox / "x.eml").write_bytes(eml())
    api = FakeClient([RuntimeError("boom")])
    ids = process_replies(world.inbox, world.reminders, world.recovery, world.source, api,
                          "Atelier Lumière", "atelier.fr", TODAY)
    stored = world.recovery.get_reply(ids[0])
    assert stored["status"] is ReplyStatus.TO_REVIEW and stored["proposed_action"] is ActionKind.HUMAN


def test_already_processed_messages_are_not_reprocessed(world):
    world.receive(reading(ReplyIntent.DISPUTE))
    api = FakeClient([])
    assert process_replies(world.inbox, world.reminders, world.recovery, world.source, api,
                           "Atelier Lumière", "atelier.fr", TODAY) == []
    assert api.messages.calls == []


def test_dismiss_does_not_touch_holds_and_acts_once(world):
    rid = world.receive(reading(ReplyIntent.DISPUTE))
    assert dismiss_reply(rid, world.recovery, TODAY) is True
    assert dismiss_reply(rid, world.recovery, TODAY) is False
    assert world.status(rid) is ReplyStatus.DISMISSED
    assert world.recovery.active_holds(TODAY) == {}
