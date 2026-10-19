import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from agent_relances.models import ActionKind, PlanStatus, ReplyIntent, ReplyStatus
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore

TODAY = date(2026, 10, 16)


@pytest.fixture
def store():
    return RecoveryStore(connect())


def add_reply(store, message_id="<m1@x>", **overrides):
    values = dict(
        message_id=message_id, client_key="hotel-canuts", reminder_ref="R-00042", match_method="reference",
        sender="compta@hotel-canuts.fr", subject="Re: Relance [Réf. R-00042]", received_at="2026-10-15T09:30:00",
        body="Nous paierons le 30.", intent=ReplyIntent.PROMISED_DATE, promised_date=date(2026, 10, 30),
        requested_installments=None, summary="Promet un paiement le 30.", suspicious=False,
        proposed_action=ActionKind.POSTPONE, action_until=date(2026, 11, 2), status=ReplyStatus.TO_REVIEW,
        cost_usd=0.002, today=TODAY,
    )
    values.update(overrides)
    return store.add_reply(**values)


def test_reply_round_trip_and_dedup_by_message_id(store):
    reply_id = add_reply(store)
    reply = store.get_reply(reply_id)
    assert reply["intent"] is ReplyIntent.PROMISED_DATE
    assert reply["promised_date"] == date(2026, 10, 30)
    assert reply["action_until"] == date(2026, 11, 2)
    assert reply["proposed_action"] is ActionKind.POSTPONE
    assert reply["status"] is ReplyStatus.TO_REVIEW
    assert reply["suspicious"] is False
    assert reply["cost_usd"] == 0.002
    assert store.known_reply_ids() == {"<m1@x>"}
    with pytest.raises(ValueError):
        add_reply(store)
    assert len(store.list_replies()) == 1


def test_reply_status_changes_only_from_to_review(store):
    reply_id = add_reply(store)
    other = add_reply(store, "<m2@x>", client_key=None, status=ReplyStatus.UNMATCHED)
    assert store.set_reply_status(reply_id, ReplyStatus.APPLIED) is True
    assert store.set_reply_status(reply_id, ReplyStatus.DISMISSED) is False
    assert store.get_reply(reply_id)["status"] is ReplyStatus.APPLIED
    assert store.set_reply_status(other, ReplyStatus.APPLIED) is False
    assert [r["id"] for r in store.list_replies([ReplyStatus.UNMATCHED])] == [other]
    assert [r["id"] for r in store.list_replies([ReplyStatus.TO_REVIEW])] == []


def test_holds_expire_open_ended_suspension_and_release(store):
    store.add_hold("hotel-canuts", "report", TODAY + timedelta(days=7), "paiement annoncé", TODAY)
    store.add_hold("boulangerie", "report", TODAY - timedelta(days=1), "ancien", TODAY - timedelta(days=8))
    suspension = store.add_hold("atelier", "suspension", None, "contestation", TODAY)
    assert store.active_holds(TODAY) == {"hotel-canuts": date(2026, 10, 23), "atelier": None}
    assert "hotel-canuts" not in store.active_holds(date(2026, 10, 23))  # le jour de la fin, la pause est finie
    assert store.release_hold(suspension, TODAY) is True
    assert store.release_hold(suspension, TODAY) is False
    assert store.active_holds(TODAY) == {"hotel-canuts": date(2026, 10, 23)}


def test_suspension_wins_over_report_for_same_client(store):
    store.add_hold("atelier", "report", TODAY + timedelta(days=7), "", TODAY)
    store.add_hold("atelier", "suspension", None, "", TODAY)
    assert store.active_holds(TODAY) == {"atelier": None}


def test_create_plan_refuses_second_open_plan_and_bad_amounts(store):
    amounts = [Decimal("250"), Decimal("250")]
    plan_id = store.create_plan("hotel-canuts", [1, 2], Decimal("500"), amounts, TODAY)
    plan = store.get_plan(plan_id)
    assert plan.status is PlanStatus.PROPOSED
    assert plan.invoice_ids == [1, 2]
    assert plan.total == Decimal("500")
    assert [i.amount for i in plan.installments] == amounts
    assert all(i.due_date is None for i in plan.installments)
    assert store.open_plan_for("hotel-canuts").id == plan_id
    with pytest.raises(ValueError, match="déjà"):
        store.create_plan("hotel-canuts", [3], Decimal("300"), [Decimal("150")] * 2, TODAY)
    with pytest.raises(ValueError, match="non conforme"):
        store.create_plan("atelier", [4], Decimal("500"), [Decimal("100")] * 5, TODAY)
    assert store.open_plan_for("atelier") is None
    assert len(store.list_plans()) == 1


def test_new_plan_allowed_once_previous_is_closed(store):
    first = store.create_plan("hotel-canuts", [1], Decimal("300"), [Decimal("150")] * 2, TODAY)
    assert store.set_plan_status(first, PlanStatus.CANCELLED, TODAY) is True
    second = store.create_plan("hotel-canuts", [1], Decimal("300"), [Decimal("150")] * 2, TODAY)
    assert second != first and store.open_plan_for("hotel-canuts").id == second


def test_activate_plan_sets_dates_and_refuses_second_activation(store):
    plan_id = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    assert store.activate_plan(plan_id, date(2026, 10, 16)) is True
    plan = store.get_plan(plan_id)
    assert plan.status is PlanStatus.ACTIVE
    assert plan.agreed_on == date(2026, 10, 16)
    assert [i.due_date for i in plan.installments] == [date(2026, 11, 15), date(2026, 12, 15)]
    assert store.activate_plan(plan_id, date(2027, 1, 1)) is False
    assert store.get_plan(plan_id).agreed_on == date(2026, 10, 16)
    assert store.get_plan(plan_id).installments[0].due_date == date(2026, 11, 15)


def test_mark_received_and_remaining(store):
    plan_id = store.create_plan("hotel-canuts", [1], Decimal("500.01"), [Decimal("250"), Decimal("250.01")], TODAY)
    assert store.mark_received(plan_id, 1, date(2026, 11, 20)) is False  # pas encore en cours
    store.activate_plan(plan_id, TODAY)
    assert store.get_plan(plan_id).remaining == Decimal("500.01")
    assert store.mark_received(plan_id, 1, date(2026, 11, 20)) is True
    assert store.mark_received(plan_id, 1, date(2026, 11, 21)) is False
    plan = store.get_plan(plan_id)
    assert plan.installments[0].received_on == date(2026, 11, 20)
    assert plan.remaining == Decimal("250.01")
    assert store.mark_received(plan_id, 3, TODAY) is False


def test_set_plan_status_rules(store):
    plan_id = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    assert store.set_plan_status(plan_id, PlanStatus.BROKEN, TODAY) is False  # pas en cours
    assert store.set_plan_status(plan_id, PlanStatus.ACTIVE, TODAY) is False
    store.activate_plan(plan_id, TODAY)
    assert store.set_plan_status(plan_id, PlanStatus.BROKEN, TODAY) is True
    assert store.set_plan_status(plan_id, PlanStatus.CANCELLED, TODAY) is False
    assert store.get_plan(plan_id).status is PlanStatus.BROKEN
    assert store.list_plans([PlanStatus.BROKEN])[0].id == plan_id
    assert store.list_plans([PlanStatus.ACTIVE]) == []


def test_planned_invoice_ids_ignore_cancelled_plans_and_keep_settled_ones(store):
    open_plan = store.create_plan("hotel-canuts", [1, 2], Decimal("500"), [Decimal("250")] * 2, TODAY)
    cancelled = store.create_plan("atelier", [3], Decimal("300"), [Decimal("150")] * 2, TODAY)
    store.set_plan_status(cancelled, PlanStatus.CANCELLED, TODAY)
    assert store.planned_invoice_ids() == {1, 2}
    store.activate_plan(open_plan, TODAY)
    assert store.planned_invoice_ids() == {1, 2}
    store.set_plan_status(open_plan, PlanStatus.SETTLED, TODAY)
    assert store.planned_invoice_ids() == {1, 2}       # soldé par mensualités : jamais de retour aux relances


def test_installment_reminders_and_disputes(store):
    plan_id = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    store.record_installment_reminder(plan_id, 1, 2, date(2026, 12, 1), "/tmp/b.eml")
    store.record_installment_reminder(plan_id, 1, 1, date(2026, 11, 20), None)
    assert store.installment_reminders(plan_id) == {1: [(1, date(2026, 11, 20)), (2, date(2026, 12, 1))]}
    assert store.installment_reminders(999) == {}
    dispute_id = store.add_dispute("hotel-canuts", "data/contentieux/hotel-canuts_2026-12-20", Decimal("250.50"), TODAY)
    [dispute] = store.list_disputes()
    assert dispute["id"] == dispute_id
    assert dispute["total_due"] == Decimal("250.50")
    assert dispute["created_on"] == TODAY


def test_connect_migrates_existing_v1_database_without_loss(tmp_path):
    path = str(tmp_path / "v1.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE clients (key TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',"
        " contact TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', professional INTEGER NOT NULL DEFAULT 1,"
        " updated_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "INSERT INTO clients (key, name) VALUES ('hotel-canuts', 'Hôtel des Canuts');"
    )
    raw.commit()
    raw.close()
    conn = connect(path)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"replies", "holds", "plans", "plan_invoices", "installments", "installment_reminders", "disputes",
            "reminders"} <= tables
    assert conn.execute("SELECT name FROM clients").fetchone()["name"] == "Hôtel des Canuts"
    store = RecoveryStore(conn)
    assert store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY) == 1
    connect(path)  # idempotent


def test_add_reply_normalizes_client_key_and_reports_real_integrity_errors(store):
    reply_id = add_reply(store, client_key="Hôtel des Canuts")
    assert store.get_reply(reply_id)["client_key"] == "hotel des canuts"
    assert store.get_reply(add_reply(store, "<m2@x>", client_key=None))["client_key"] is None
    with pytest.raises(sqlite3.IntegrityError):  # NOT NULL, pas pris pour un doublon
        add_reply(store, "<m3@x>", sender=None)
    with pytest.raises(ValueError, match="déjà"):
        add_reply(store, "<m1@x>")


def test_installment_reminder_keeps_first_decision(store):
    plan_id = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    assert store.record_installment_reminder(plan_id, 1, 1, date(2026, 11, 20), "/a.eml") is True
    assert store.record_installment_reminder(plan_id, 1, 1, date(2026, 12, 25), "/b.eml") is False
    assert store.installment_reminders(plan_id) == {1: [(1, date(2026, 11, 20))]}
    row = store._conn.execute("SELECT eml_path FROM installment_reminders").fetchone()
    assert row["eml_path"] == "/a.eml"


def test_unique_index_backstops_second_open_plan(store, monkeypatch):
    first = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    monkeypatch.setattr(store, "open_plan_for", lambda key: None)  # contourne la vérification applicative
    with pytest.raises(ValueError, match="déjà"):
        store.create_plan("hotel-canuts", [2], Decimal("300"), [Decimal("150")] * 2, TODAY)
    assert [p.id for p in store.list_plans()] == [first]
    assert store.planned_invoice_ids() == {1}


def test_add_hold_rejects_report_without_end_and_unknown_kind(store):
    with pytest.raises(ValueError, match="fin"):
        store.add_hold("hotel-canuts", "report", None, "", TODAY)
    with pytest.raises(ValueError, match="inconnu"):
        store.add_hold("hotel-canuts", "autre", None, "", TODAY)
    assert store.active_holds(TODAY) == {}


def test_list_active_holds_gives_each_hold_with_its_id(store):
    report = store.add_hold("hotel-canuts", "report", TODAY + timedelta(days=7), "paiement annoncé", TODAY)
    store.add_hold("boulangerie", "report", TODAY, "fini aujourd'hui", TODAY - timedelta(days=7))
    released = store.add_hold("atelier", "suspension", None, "contestation", TODAY)
    store.release_hold(released, TODAY)
    [hold] = store.list_active_holds(TODAY)
    assert hold == {"id": report, "client_key": "hotel-canuts", "kind": "report", "until": date(2026, 10, 23),
                    "reason": "paiement annoncé", "created_on": TODAY}


def test_attach_reply_only_from_unmatched_and_only_once(store):
    reply_id = add_reply(store, client_key=None, status=ReplyStatus.UNMATCHED, proposed_action=ActionKind.NONE,
                         cost_usd=0.0)
    fields = dict(intent=ReplyIntent.DISPUTE, promised_date=None, requested_installments=None, summary="Conteste.",
                  suspicious=False, proposed_action=ActionKind.SUSPEND, action_until=None, cost_usd=0.003)
    assert store.attach_reply(reply_id, "Hôtel des Canuts", **fields) is True
    reply = store.get_reply(reply_id)
    assert reply["client_key"] == "hotel des canuts" and reply["match_method"] == "manuel"
    assert reply["status"] is ReplyStatus.TO_REVIEW and reply["proposed_action"] is ActionKind.SUSPEND
    assert reply["intent"] is ReplyIntent.DISPUTE and reply["cost_usd"] == pytest.approx(0.003)
    assert store.attach_reply(reply_id, "Autre client", **fields) is False
    assert store.get_reply(reply_id)["client_key"] == "hotel des canuts"
    matched = add_reply(store, message_id="<m2@x>")
    assert store.attach_reply(matched, "Autre client", **fields) is False


def test_active_plan_with_a_received_installment_cannot_be_cancelled(store):
    plan_id = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    store.activate_plan(plan_id, TODAY)
    store.mark_received(plan_id, 1, TODAY)
    assert store.set_plan_status(plan_id, PlanStatus.CANCELLED, TODAY) is False
    assert store.get_plan(plan_id).status is PlanStatus.ACTIVE
    other = store.create_plan("atelier", [2], Decimal("500"), [Decimal("250")] * 2, TODAY)
    store.activate_plan(other, TODAY)
    assert store.set_plan_status(other, PlanStatus.CANCELLED, TODAY) is True   # rien reçu : annulable


def test_last_installment_contacts_per_client(store):
    first = store.create_plan("hotel-canuts", [1], Decimal("500"), [Decimal("250")] * 2, TODAY)
    store.record_installment_reminder(first, 1, 1, date(2026, 11, 20), None)
    store.record_installment_reminder(first, 1, 2, date(2026, 12, 2), None)
    store.set_plan_status(first, PlanStatus.CANCELLED, TODAY)
    second = store.create_plan("hotel-canuts", [2], Decimal("500"), [Decimal("250")] * 2, TODAY)
    store.record_installment_reminder(second, 1, 1, date(2026, 11, 25), None)
    assert store.last_installment_contacts() == {"hotel-canuts": date(2026, 12, 2)}


def test_unique_index_backstops_a_second_dispute(store):
    store.add_dispute("hotel-canuts", "a", Decimal("10"), TODAY)
    with pytest.raises(ValueError, match="déjà"):
        store.add_dispute("Hotel-Canuts", "b", Decimal("10"), TODAY)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("INSERT INTO disputes (client_key, folder, created_on, total_due) VALUES"
                            " ('hotel-canuts', 'c', '2026-10-16', '1')")
    assert len(store.list_disputes()) == 1


def test_existing_database_gets_the_dispute_index(tmp_path):
    path = tmp_path / "ancienne.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE disputes (id INTEGER PRIMARY KEY AUTOINCREMENT, client_key TEXT NOT NULL,"
                " folder TEXT NOT NULL, created_on TEXT NOT NULL, total_due TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = connect(str(path))
    indexes = {row["name"]: row["unique"] for row in conn.execute("PRAGMA index_list(disputes)")}
    assert indexes.get("one_dispute_per_client") == 1
