from decimal import Decimal

from agent_relances.metrics import dashboard
from agent_relances.models import Stage, Tone
from agent_relances.prepare import prepare_today
from agent_relances.sources import InvoiceSource
from agent_relances.models import ActionKind, PlanStatus, ReplyIntent, ReplyReading
from agent_relances.replies.actions import propose_action, reply_is_suspicious
from agent_relances.replies.matching import fetch_replies
from agent_relances.rules.installments import dispute_due
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from demo.seed import DEMO_TODAY, seed


def test_demo_shows_every_situation(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    conn = connect(str(relances))
    source, store = InvoiceSource.open_readonly(factures), ReminderStore(conn)
    result = prepare_today(source, store, None, "Atelier Lumière SAS", DEMO_TODAY, recovery=RecoveryStore(conn))
    found = {p.due.client.name: (p.due.stage, p.due.tone, p.due.legal_notice, len(p.due.invoices))
             for p in result.prepared}
    assert found == {
        "Hôtel des Canuts": (Stage.COURTEOUS, Tone.LIGHT, False, 1),
        "Hôtel Bellevue": (Stage.COURTEOUS, Tone.NEUTRAL, False, 2),
        "Bistrot Saint-Paul": (Stage.FIRM, Tone.NEUTRAL, False, 1),
        "Galerie Nova": (Stage.FINAL, Tone.FIRM, True, 1),
        "Sophie Leroy": (Stage.FINAL, Tone.FIRM, False, 1),
    }
    assert result.missing_clients == ["Studio Cohen"]
    assert [i.number for i in result.escalated] == ["AL-2026-088"]
    assert dashboard(source, store, DEMO_TODAY).recovered_total == Decimal("860.00")


def test_seed_replaces_previous_demo_files(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    seed(factures, relances)
    assert len(ReminderStore(connect(str(relances))).list_clients()) == 10


def test_historical_reminders_are_fallback_texts_outside_the_metrics(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    store = ReminderStore(connect(str(relances)))
    history = store.list_reminders()
    assert history and all(r.source.value == "secours" and r.origin == "historique" for r in history if r.body)
    board = dashboard(InvoiceSource.open_readonly(factures), store, DEMO_TODAY)
    assert board.fallback_rate is None and board.mean_cost_usd == 0.0


def test_demo_inbox_has_six_replies_attached_to_the_right_client(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    conn = connect(str(relances))
    store, recovery = ReminderStore(conn), RecoveryStore(conn)
    found = fetch_replies(tmp_path / "demo_inbox", store, recovery)
    by_client = {match.client_key: (email, match) for email, match in found}
    assert len(found) == 6
    assert set(by_client) == {"bistrot saint-paul", "galerie nova", "sophie leroy", "restaurant le gourmet",
                              "boulangerie faure", "imprimerie dupont"}
    assert all(match.method == "reference" for _, match in found)
    assert by_client["imprimerie dupont"][1].reference.startswith("E-")
    for key, (email, _) in by_client.items():
        assert reply_is_suspicious(email, [store.get_client(key).email], None) is (key == "boulangerie faure")


def test_demo_plans_and_dispute(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    recovery = RecoveryStore(connect(str(relances)))
    active = recovery.open_plan_for("traiteur marchand")
    proposed = recovery.open_plan_for("imprimerie dupont")
    assert active.status is PlanStatus.ACTIVE and proposed.status is PlanStatus.PROPOSED
    first = active.installments[0]
    assert (DEMO_TODAY - first.due_date).days == 40 and first.received_on is None
    done = recovery.installment_reminders(active.id)
    assert [stage for stage, _ in done[1]] == [1, 2, 3]
    assert (DEMO_TODAY - done[1][-1][1]).days >= 7
    assert dispute_due(active, done, DEMO_TODAY)
    assert not dispute_due(proposed, recovery.installment_reminders(proposed.id), DEMO_TODAY)


def test_demo_seed_twice_keeps_six_replies(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    seed(factures, relances)
    assert len(list((tmp_path / "demo_inbox").glob("*.eml"))) == 6


def test_demo_accord_reply_can_activate_the_proposed_plan(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    proposed = RecoveryStore(connect(str(relances))).open_plan_for("imprimerie dupont")
    reading = ReplyReading(intent=ReplyIntent.PLAN_AGREEMENT, summary="ok")
    assert propose_action(reading, False, True, proposed, DEMO_TODAY).kind is ActionKind.ACTIVATE_PLAN
