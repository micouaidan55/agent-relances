from datetime import date
from decimal import Decimal
from email.message import EmailMessage

import pytest

from agent_relances.models import ActionKind, PlanStatus, ReplyIntent, ReplyReading, ReplyStatus
from agent_relances.replies.process import (
    attach_reply, confirm_reply, dismiss_reply, process_replies, record_agreement,
)
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from agent_relances.models import ReminderDraft
from agent_relances.replies import process as confirm_module
from tests.factories import client, due_reminder, factures_conn, issued
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
    assert [e.name for e in emls] == [f"echeancier_{plan.id:05d}_proposition.eml"]
    assert f"E-{plan.id:05d}" in emls[0].read_text(errors="ignore")


def test_agreement_activates_the_proposed_plan_with_dates(world):
    world.confirm(world.receive(reading(ReplyIntent.PLAN_REQUEST), message_id="<a@x>"))
    rid = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<b@x>", body="D'accord.")
    assert world.recovery.get_reply(rid)["proposed_action"] is ActionKind.ACTIVATE_PLAN
    assert world.confirm(rid) == []
    plan = world.recovery.open_plan_for(KEY)
    assert plan.status is PlanStatus.ACTIVE and plan.agreed_on == TODAY
    assert plan.installments[0].due_date == date(2026, 11, 15)
    names = sorted(p.name for p in world.outbox.glob("*.eml"))
    assert names == [f"echeancier_{plan.id:05d}_confirmation.eml", f"echeancier_{plan.id:05d}_proposition.eml"]
    assert "15/11/2026" in (world.outbox / names[0]).read_text(errors="ignore")


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


def test_failed_activation_deposit_is_retried_on_a_later_day(world, monkeypatch):
    world.confirm(world.receive(reading(ReplyIntent.PLAN_REQUEST), message_id="<a@x>"))
    rid = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<b@x>")
    real = confirm_module.deposit_plan_draft
    monkeypatch.setattr(confirm_module, "deposit_plan_draft", lambda *a, **k: (_ for _ in ()).throw(OSError("panne")))
    assert world.confirm(rid)
    assert world.status(rid) is ReplyStatus.TO_REVIEW
    assert world.recovery.open_plan_for(KEY).status is PlanStatus.ACTIVE
    monkeypatch.setattr(confirm_module, "deposit_plan_draft", real)
    assert confirm_reply(rid, world.reminders, world.recovery, world.source, world.outbox, None, "Atelier Lumière",
                         date(2026, 10, 20)) == []
    plan = world.recovery.open_plan_for(KEY)
    assert (world.outbox / f"echeancier_{plan.id:05d}_confirmation.eml").exists()
    assert plan.agreed_on == TODAY


def test_second_agreement_after_confirmation_deposited_creates_no_duplicate(world):
    world.confirm(world.receive(reading(ReplyIntent.PLAN_REQUEST), message_id="<a@x>"))
    first = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<b@x>")
    second = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<c@x>")
    assert world.confirm(first) == []
    errors = world.confirm(second)
    assert errors and "déjà actif" in errors[0]
    assert world.status(second) is ReplyStatus.TO_REVIEW
    assert len(list(world.outbox.glob("*_confirmation.eml"))) == 1


def test_stale_postpone_is_refused_and_writes_nothing(world):
    rid = world.receive(reading(ReplyIntent.PROMISED_DATE, "2026-10-30"))
    errors = confirm_reply(rid, world.reminders, world.recovery, world.source, world.outbox, None, "Atelier Lumière",
                           date(2026, 11, 2))
    assert errors and "dépassée" in errors[0]
    assert world.status(rid) is ReplyStatus.TO_REVIEW
    assert world.recovery.active_holds(date(2026, 11, 1)) == {}


def test_unexpected_error_after_transition_restores_status(world, monkeypatch):
    rid = world.receive(reading(ReplyIntent.DISPUTE))
    monkeypatch.setattr(world.recovery, "add_hold", lambda *a, **k: (_ for _ in ()).throw(KeyError("boum")))
    assert world.confirm(rid)
    assert world.status(rid) is ReplyStatus.TO_REVIEW


def test_look_alike_sender_gives_human(world):
    world.reminders.add_validated(due_reminder(), ReminderDraft(subject="Relance", body="Bonjour"), TODAY)
    rid = world.receive(reading(ReplyIntent.PAYMENT_ANNOUNCED), subject="Re: Relance [Réf. R-00001]",
                        sender="compta@hotel-canuts.co", body="Voici nos nouvelles coordonnées bancaires, IBAN FR76...")
    stored = world.recovery.get_reply(rid)
    assert stored["suspicious"] is True and stored["proposed_action"] is ActionKind.HUMAN


def test_not_yet_due_invoice_enters_total_and_plan(tmp_path):
    world = World(tmp_path, amounts=("100.00", "150.00"))
    world.ids.append(world.repo.add(issued("AL-F", amount="200.00", due=date(2026, 12, 1)), "f.pdf"))
    rid = world.receive(reading(ReplyIntent.PLAN_REQUEST))
    assert world.confirm(rid) == []
    plan = world.recovery.open_plan_for(KEY)
    assert plan.total == D("450.00") and sorted(plan.invoice_ids) == sorted(world.ids)


def test_unpaid_without_due_date_uses_issue_date():
    conn, repo = factures_conn()
    repo.add(issued("AL-9", due=None, issue=date(2026, 9, 1)), "a.pdf")
    invoice, = InvoiceSource(conn).unpaid_of(KEY, TODAY)
    assert invoice.due_date == date(2026, 9, 1) and invoice.days_late == 45


def test_unmatched_reply_is_kept_without_model_call(world):
    (world.inbox / "x.eml").write_bytes(eml(sender="x@inconnu.io", subject="Re: [Réf. R-99999]"))
    api = FakeClient([])
    ids = process_replies(world.inbox, world.reminders, world.recovery, world.source, api,
                          "Atelier Lumière", "atelier.fr", TODAY)
    stored = world.recovery.get_reply(ids[0])
    assert stored["status"] is ReplyStatus.UNMATCHED and stored["client_key"] is None
    assert stored["cost_usd"] == 0
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


def test_without_api_the_attached_reply_is_stored_for_a_human(world):
    (world.inbox / "x.eml").write_bytes(eml())
    ids = process_replies(world.inbox, world.reminders, world.recovery, world.source, None,
                          "Atelier Lumière", "atelier.fr", TODAY)
    stored = world.recovery.get_reply(ids[0])
    assert stored["status"] is ReplyStatus.TO_REVIEW and stored["proposed_action"] is ActionKind.HUMAN
    assert stored["cost_usd"] == 0


def test_unmatched_reply_is_checked_for_fraud_against_every_client_card(world):
    world.reminders.save_client(client(name="Galerie Nova", email="compta@galerie-nova.fr", contact="Marc"))
    (world.inbox / "x.eml").write_bytes(eml(sender="compta@galerie-nova.co", subject="Re: Relance [Réf. R-99999]",
                                            body="Notre nouveau RIB : IBAN FR76 3000 6000 0112 3456 7890 189."))
    api = FakeClient([])
    ids = process_replies(world.inbox, world.reminders, world.recovery, world.source, api,
                          "Atelier Lumière", "atelier.fr", TODAY)
    stored = world.recovery.get_reply(ids[0])
    assert stored["status"] is ReplyStatus.UNMATCHED and stored["suspicious"] is True
    assert api.messages.calls == []


def test_clean_unmatched_reply_is_not_flagged(world):
    (world.inbox / "x.eml").write_bytes(eml(sender="x@inconnu.io", subject="Re: [Réf. R-99999]"))
    ids = process_replies(world.inbox, world.reminders, world.recovery, world.source, None,
                          "Atelier Lumière", "atelier.fr", TODAY)
    assert world.recovery.get_reply(ids[0])["suspicious"] is False


def test_matched_reply_from_a_look_alike_of_another_client_is_suspicious(world):
    world.reminders.save_client(client(name="Galerie Nova", email="compta@galerie-nova.fr", contact="Marc"))
    world.reminders.add_validated(due_reminder(), ReminderDraft(subject="Relance", body="Bonjour"), TODAY)
    rid = world.receive(reading(ReplyIntent.PAYMENT_ANNOUNCED), subject="Re: Relance [Réf. R-00001]",
                        sender="compta@galerie-nova.co", body="Virement parti.")
    stored = world.recovery.get_reply(rid)
    assert stored["suspicious"] is True and stored["proposed_action"] is ActionKind.HUMAN


def test_reply_from_a_client_in_dispute_is_proposed_to_a_human(world):
    world.recovery.add_dispute(KEY, "dossier", D("450.00"), TODAY)
    rid = world.receive(reading(ReplyIntent.PAYMENT_ANNOUNCED), body="Virement parti.")
    assert world.recovery.get_reply(rid)["proposed_action"] is ActionKind.HUMAN


def propose(world) -> int:
    assert world.confirm(world.receive(reading(ReplyIntent.PLAN_REQUEST), message_id="<a@x>")) == []
    return world.recovery.open_plan_for(KEY).id


def agree_by_hand(world, plan_id, on=TODAY) -> list[str]:
    return record_agreement(plan_id, world.reminders, world.recovery, world.source, world.outbox, None,
                            "Atelier Lumière", on)


def test_agreement_recorded_by_hand_activates_and_deposits_the_confirmation(world):
    plan_id = propose(world)
    assert agree_by_hand(world, plan_id) == []
    plan = world.recovery.get_plan(plan_id)
    assert plan.status is PlanStatus.ACTIVE and plan.agreed_on == TODAY
    assert plan.installments[0].due_date == date(2026, 11, 15)
    confirmation = world.outbox / f"echeancier_{plan_id:05d}_confirmation.eml"
    assert "15/11/2026" in confirmation.read_text(errors="ignore")
    assert agree_by_hand(world, plan_id)                         # deuxième accord : refusé, rien de nouveau
    assert len(list(world.outbox.glob("*_confirmation.eml"))) == 1


def test_agreement_by_hand_uses_the_same_path_as_a_reply(world, monkeypatch):
    plan_id = propose(world)
    calls = []
    real = confirm_module._activate_and_confirm
    monkeypatch.setattr(confirm_module, "_activate_and_confirm", lambda *a, **k: calls.append(a) or real(*a, **k))
    assert agree_by_hand(world, plan_id) == []
    assert len(calls) == 1


def test_agreement_by_hand_is_refused_when_a_covered_invoice_was_paid(world):
    plan_id = propose(world)
    world.repo.mark_paid(world.ids[0], date(2026, 10, 15))
    errors = agree_by_hand(world, plan_id)
    assert errors and "annule" in errors[0].lower() and "nouveau" in errors[0]
    assert world.recovery.get_plan(plan_id).status is PlanStatus.PROPOSED
    assert not list(world.outbox.glob("*_confirmation.eml"))


def test_agreement_reply_is_refused_when_a_covered_invoice_was_paid(world):
    propose(world)
    rid = world.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<b@x>")
    world.repo.mark_paid(world.ids[1], date(2026, 10, 15))
    errors = world.confirm(rid)
    assert errors and "annule" in errors[0].lower()
    assert world.recovery.open_plan_for(KEY).status is PlanStatus.PROPOSED
    assert world.status(rid) is ReplyStatus.TO_REVIEW


def test_agreement_by_hand_on_a_cancelled_plan_is_refused(world):
    plan_id = propose(world)
    world.recovery.set_plan_status(plan_id, PlanStatus.CANCELLED, TODAY)
    assert agree_by_hand(world, plan_id)
    assert world.recovery.get_plan(plan_id).status is PlanStatus.CANCELLED


def unmatched(world, sender="x@inconnu.io", body="Bonjour") -> int:
    (world.inbox / "u.eml").write_bytes(eml(message_id="<u@x>", sender=sender, subject="Re: [Réf. R-99999]",
                                            body=body))
    [reply_id] = process_replies(world.inbox, world.reminders, world.recovery, world.source, None,
                                 "Atelier Lumière", "atelier.fr", TODAY)
    return reply_id


def attach(world, reply_id, api, key="Hôtel des Canuts") -> list[str]:
    return attach_reply(reply_id, key, world.reminders, world.recovery, world.source, api, "Atelier Lumière",
                        "atelier.fr", TODAY)


def test_attaching_an_unmatched_reply_reads_it_and_proposes_an_action(world):
    reply_id = unmatched(world, body="Nous paierons le 30 octobre.")
    api = FakeClient([reply(reading(ReplyIntent.PROMISED_DATE, "2026-10-30"))])
    assert attach(world, reply_id, api) == []
    stored = world.recovery.get_reply(reply_id)
    assert stored["status"] is ReplyStatus.TO_REVIEW and stored["client_key"] == KEY
    assert stored["match_method"] == "manuel" and stored["proposed_action"] is ActionKind.POSTPONE
    assert stored["action_until"] == date(2026, 11, 2) and stored["cost_usd"] > 0
    assert len(api.messages.calls) == 1
    assert world.confirm(reply_id) == []
    assert world.recovery.active_holds(TODAY) == {KEY: date(2026, 11, 2)}


def test_attaching_without_api_goes_to_a_human(world):
    reply_id = unmatched(world)
    assert attach(world, reply_id, None) == []
    assert world.recovery.get_reply(reply_id)["proposed_action"] is ActionKind.HUMAN


def test_attaching_keeps_the_fraud_flag_and_proposes_a_human(world):
    reply_id = unmatched(world, sender="compta@hotel-canuts.co", body="Nouveau RIB : IBAN FR76 3000 6000 0112 3456 7890 189.")
    assert world.recovery.get_reply(reply_id)["suspicious"] is True
    assert attach(world, reply_id, FakeClient([reply(reading(ReplyIntent.PAYMENT_ANNOUNCED))])) == []
    stored = world.recovery.get_reply(reply_id)
    assert stored["suspicious"] is True and stored["proposed_action"] is ActionKind.HUMAN


def test_attaching_twice_or_to_an_unknown_client_is_refused(world):
    reply_id = unmatched(world)
    assert attach(world, reply_id, None, key="Inconnu SARL")
    assert world.status(reply_id) is ReplyStatus.UNMATCHED
    assert attach(world, reply_id, None) == []
    assert attach(world, reply_id, None)


# --- dérogation exceptionnelle au plafond de 4 mensualités ---

MOTIF = "Client en difficulté, accord de la direction"


@pytest.fixture
def big_world(tmp_path):
    return World(tmp_path, amounts=("500.00", "500.00", "500.00"))        # 1 500 € dus


def _request(w):
    return w.receive(reading(ReplyIntent.PLAN_REQUEST))


def test_more_than_four_installments_without_reason_is_refused(big_world):
    w = big_world
    rid = _request(w)
    for reason in (None, "", "   "):
        errors = w.confirm(rid, installments=6, exception_reason=reason)
        assert errors and "dérogation" in errors[0] and "motif" in errors[0]
    assert w.recovery.list_plans() == [] and w.status(rid) is ReplyStatus.TO_REVIEW
    assert not w.outbox.exists() or not list(w.outbox.glob("*.eml"))


def test_exception_with_reason_creates_the_plan_stores_and_journals_it(big_world):
    w = big_world
    rid = _request(w)
    assert w.confirm(rid, installments=6, exception_reason=f"  {MOTIF}  ") == []
    [plan] = w.recovery.list_plans()
    assert len(plan.installments) == 6 and plan.exception_reason == MOTIF
    assert sum(i.amount for i in plan.installments) == D("1500.00")
    assert list(w.outbox.glob("echeancier_*_proposition.eml"))
    entries = [e for e in w.reminders.list_log() if e["action"] == "reponse_appliquee"]
    assert entries[0]["detail"]["derogation"] == MOTIF


def test_exception_reaches_twelve_but_not_thirteen(tmp_path):
    w = World(tmp_path, amounts=("1000.00", "500.00"))
    rid = _request(w)
    errors = w.confirm(rid, installments=13, exception_reason=MOTIF)
    assert errors and "Trop de versements" in errors[0] and w.recovery.list_plans() == []
    assert w.status(rid) is ReplyStatus.TO_REVIEW
    assert w.confirm(rid, installments=12, exception_reason=MOTIF) == []
    assert len(w.recovery.list_plans()[0].installments) == 12


def test_exception_keeps_the_hundred_euro_minimum(tmp_path):
    w = World(tmp_path, amounts=("300.00", "300.00"))                          # 600 € dus : 6 x 100 € au plus
    rid = _request(w)
    errors = w.confirm(rid, installments=7, exception_reason=MOTIF)
    assert errors and "Trop de versements" in errors[0]
    assert w.confirm(rid, installments=6, exception_reason=MOTIF) == []
    assert all(i.amount == D("100.00") for i in w.recovery.list_plans()[0].installments)


def test_reason_is_ignored_when_four_or_fewer_installments(big_world):
    w = big_world
    rid = _request(w)
    assert w.confirm(rid, installments=3, exception_reason=MOTIF) == []
    plan = w.recovery.list_plans()[0]
    assert len(plan.installments) == 3 and plan.exception_reason is None
    assert "derogation" not in w.reminders.list_log()[0]["detail"]


def test_default_count_never_exceeds_four_even_with_a_reason(big_world):
    w = big_world
    rid = w.receive(reading(ReplyIntent.PLAN_REQUEST, installments=10))
    assert w.confirm(rid, exception_reason=MOTIF) == []
    plan = w.recovery.list_plans()[0]
    assert len(plan.installments) == 4 and plan.exception_reason is None


def test_exception_plan_activates_and_confirmation_is_deposited(big_world):
    w = big_world
    w.confirm(_request(w), installments=6, exception_reason=MOTIF)
    agreement = w.receive(reading(ReplyIntent.PLAN_AGREEMENT), message_id="<b@x>")
    assert w.confirm(agreement) == []
    plan = w.recovery.open_plan_for(KEY)
    assert plan.status is PlanStatus.ACTIVE and len(plan.installments) == 6
    assert len(list(w.outbox.glob("*_confirmation.eml"))) == 1
