from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agent_relances.models import ReminderStatus
from agent_relances.storage.db import connect
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, factures_conn, issued

APP_PATH = str(Path(__file__).resolve().parent.parent / "app" / "main.py")
PAGES = ["app_pages/clients.py", "app_pages/tableau_de_bord.py", "app_pages/journal_validees.py",
         "app_pages/journal_humain.py", "app_pages/journal_historique.py"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    factures = tmp_path / "factures.db"
    conn, repo = factures_conn(str(factures))
    repo.add(issued("AL-2026-101", due=date(2026, 10, 1)), "a.pdf")
    conn.close()
    relances = tmp_path / "relances.db"
    store = ReminderStore(connect(str(relances)))
    store.save_client(client())
    monkeypatch.setenv("AGENT_FACTURES_DB", str(factures))
    monkeypatch.setenv("AGENT_RELANCES_DB", str(relances))
    monkeypatch.setenv("AGENT_RELANCES_OUTBOX", str(tmp_path / "deposees"))
    monkeypatch.setenv("TODAY", "2026-10-15")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    return tmp_path, store


def run_app(page: str | None = None) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30).run()
    if page:
        at.switch_page(page).run()
    assert not at.exception
    return at


def prepare(at: AppTest) -> AppTest:
    next(b for b in at.button if b.label == "Préparer les relances du jour").click().run()
    assert not at.exception
    return at


def pending(store):
    return store.list_reminders({ReminderStatus.TO_VALIDATE})


def test_every_page_renders(env):
    run_app()
    for page in PAGES:
        run_app(page)


def test_missing_factures_database_shows_an_error(env, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_FACTURES_DB", str(tmp_path / "absente.db"))
    at = run_app()
    assert any("introuvable" in e.value for e in at.error)


def test_prepare_without_api_key_uses_the_fallback(env):
    _, store = env
    at = prepare(run_app())
    assert any("Clé d'API" in w.value for w in at.warning)
    [reminder] = pending(store)
    assert any("Hôtel des Canuts" in s.value for s in at.subheader)
    assert reminder.source.value == "secours"


def test_validate_deposits_an_eml(env):
    tmp_path, store = env
    at = prepare(run_app())
    [reminder] = pending(store)
    next(b for b in at.button if b.key == f"valider-{reminder.id}").click().run()
    assert store.get(reminder.id).status is ReminderStatus.VALIDATED
    assert list((tmp_path / "deposees").glob("*.eml"))


def test_edited_wrong_amount_is_refused(env):
    _, store = env
    at = prepare(run_app())
    [reminder] = pending(store)
    area = next(t for t in at.text_area if t.key == f"corps-{reminder.id}")
    area.set_value(reminder.body.replace("1 200,50 €", "1 250,50 €")).run()
    next(b for b in at.button if b.key == f"valider-{reminder.id}").click().run()
    assert any("Montant absent" in e.value for e in at.error)
    assert store.get(reminder.id).status is ReminderStatus.TO_VALIDATE


def test_postpone_and_hand_over(env):
    _, store = env
    at = prepare(run_app())
    [reminder] = pending(store)
    next(b for b in at.button if b.key == f"reporter-{reminder.id}").click().run()
    assert store.get(reminder.id).status is ReminderStatus.POSTPONED
    at = prepare(run_app())
    assert pending(store) == []                       # reportée : rien à préparer aujourd'hui


def test_hand_over_to_a_human(env):
    _, store = env
    at = prepare(run_app())
    [reminder] = pending(store)
    next(b for b in at.button if b.key == f"humain-{reminder.id}").click().run()
    assert store.get(reminder.id).status is ReminderStatus.HUMAN


def test_client_card_form(env):
    _, store = env
    at = run_app("app_pages/clients.py")
    at.text_input(key="fiche-nom").set_value("Galerie Nova")
    at.text_input(key="fiche-email").set_value("compta@galerie-nova.fr")
    at.checkbox(key="fiche-pro").uncheck()
    at.button(key="FormSubmitter:fiche-Enregistrer la fiche").click().run()
    saved = store.get_client("galerie nova")
    assert saved is not None and saved.email == "compta@galerie-nova.fr" and not saved.professional


def test_dashboard_metrics(env):
    at = run_app("app_pages/tableau_de_bord.py")
    labels = [m.label for m in at.metric]
    assert "Montant en retard" in labels and "Encaissé après relance" in labels
