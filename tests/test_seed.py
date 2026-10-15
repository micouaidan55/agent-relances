from decimal import Decimal

from agent_relances.metrics import dashboard
from agent_relances.models import Stage, Tone
from agent_relances.prepare import prepare_today
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.repository import ReminderStore
from demo.seed import DEMO_TODAY, seed


def test_demo_shows_every_situation(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    source, store = InvoiceSource.open_readonly(factures), ReminderStore(connect(str(relances)))
    result = prepare_today(source, store, None, "Atelier Lumière SAS", DEMO_TODAY)
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
    assert len(ReminderStore(connect(str(relances))).list_clients()) == 8


def test_historical_reminders_are_fallback_texts_outside_the_metrics(tmp_path):
    factures, relances = tmp_path / "f.db", tmp_path / "r.db"
    seed(factures, relances)
    store = ReminderStore(connect(str(relances)))
    history = store.list_reminders()
    assert history and all(r.source.value == "secours" and r.origin == "historique" for r in history if r.body)
    board = dashboard(InvoiceSource.open_readonly(factures), store, DEMO_TODAY)
    assert board.fallback_rate is None and board.mean_cost_usd == 0.0
