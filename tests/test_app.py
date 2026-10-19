from datetime import date
from decimal import Decimal
from email import message_from_bytes, policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agent_relances.models import ActionKind, PlanStatus, ReminderDraft, ReminderStatus, ReplyIntent, ReplyStatus
from agent_relances.web import md_escape
from agent_relances.rules.installments import plan_amounts
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, due_reminder, factures_conn, issued, overdue

APP_PATH = str(Path(__file__).resolve().parent.parent / "app" / "main.py")
PAGES = ["app_pages/clients.py", "app_pages/tableau_de_bord.py", "app_pages/journal_validees.py",
         "app_pages/journal_humain.py", "app_pages/journal_historique.py", "app_pages/reponses.py",
         "app_pages/echeanciers.py", "app_pages/contentieux.py"]
KEY = "hotel des canuts"


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
    monkeypatch.setenv("AGENT_RELANCES_INBOX", str(tmp_path / "inbox"))
    monkeypatch.setenv("AGENT_RELANCES_DOSSIERS", str(tmp_path / "dossiers"))
    monkeypatch.setenv("COMPANY_DOMAIN", "atelier.fr")
    monkeypatch.setenv("TODAY", "2026-10-15")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    (tmp_path / "inbox").mkdir()
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


def test_selecting_an_existing_client_prefills_the_form(env):
    _, store = env
    at = run_app("app_pages/clients.py")
    assert at.text_input(key="fiche-email").value == ""
    at.selectbox(key="fiche-choix").select("Hôtel des Canuts").run()
    assert not at.exception
    assert at.text_input(key="fiche-email").value == "compta@hotel-canuts.fr"
    assert at.text_input(key="fiche-contact").value == "Paul Girard"
    at.text_input(key="fiche-contact").set_value("Anne Morel")
    at.button(key="FormSubmitter:fiche-Enregistrer la fiche").click().run()
    assert store.get_client("hôtel des canuts").contact == "Anne Morel"
    assert store.get_client("hôtel des canuts").email == "compta@hotel-canuts.fr"
    assert at.selectbox(key="fiche-choix").value == "Nouvelle fiche" and at.text_input(key="fiche-email").value == ""
    at.selectbox(key="fiche-choix").select("Hôtel des Canuts").run()
    at.selectbox(key="fiche-choix").select("Nouvelle fiche").run()
    assert at.text_input(key="fiche-nom").value == "" and at.checkbox(key="fiche-pro").value


def test_client_history_expander_lists_the_validated_reminder(env):
    _, store = env
    store.add_validated(due_reminder(invoices=[overdue(1, "AL-2026-101")]), ReminderDraft(subject="S", body="B"),
                        date(2026, 10, 10))
    at = run_app("app_pages/clients.py")
    [expander] = [e for e in at.expander if "Hôtel des Canuts" in e.label]
    text = " ".join(m.value for m in expander.markdown)
    assert "10/10/2026" in text and "palier 1" in text and "Validée" in text and "AL" in text and "2026" in text


# --- Réponses, échéanciers, contentieux ---

TODAY = date(2026, 10, 15)


@pytest.fixture
def recovery(env):
    tmp_path, _ = env
    return RecoveryStore(connect(str(tmp_path / "relances.db")))


def write_reply(tmp_path, sender="compta@hotel-canuts.fr", subject="Re: Relance", body="Bonjour, je paie bientôt.",
                message_id="<m1@x>"):
    message = EmailMessage()
    message["From"] = f"Compta <{sender}>"
    message["To"] = "relances@atelier.fr"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Thu, 15 Oct 2026 09:30:00 +0200"
    message.set_content(body)
    (tmp_path / "inbox" / f"{message_id.strip('<>@x')}.eml").write_bytes(message.as_bytes())


def add_reply(recovery, intent=ReplyIntent.PROMISED_DATE, action=ActionKind.POSTPONE, until=date(2026, 10, 30),
              requested=None, summary="Résumé.", body="Corps.", message_id="<r1@x>") -> int:
    return recovery.add_reply(
        message_id=message_id, client_key=KEY, reminder_ref=None, match_method="expediteur",
        sender="compta@hotel-canuts.fr", subject="Re: Relance", received_at="2026-10-14T09:00:00", body=body,
        intent=intent, promised_date=None, requested_installments=requested, summary=summary, suspicious=False,
        proposed_action=action, action_until=until, status=ReplyStatus.TO_REVIEW, cost_usd=0.0, today=TODAY)


def active_plan(recovery, agreed=date(2026, 9, 5)) -> int:
    total = Decimal("1200.50")
    plan_id = recovery.create_plan(KEY, [1], total, plan_amounts(total, 4), TODAY)
    assert recovery.activate_plan(plan_id, agreed)
    return plan_id


def test_new_pages_show_their_empty_state(env):
    for page in PAGES[-3:]:
        run_app(page)


def test_collect_without_api_key_stores_replies_for_a_human(env, recovery):
    tmp_path, _ = env
    write_reply(tmp_path)
    write_reply(tmp_path, sender="inconnu@ailleurs.io", subject="Re: [Réf. R-99999]", message_id="<m2@x>")
    at = run_app("app_pages/reponses.py")
    at.button(key="relever-reponses").click().run()
    assert not at.exception
    assert any("Clé d'API absente : réponses enregistrées sans lecture automatique, à traiter par un humain" in w.value
               for w in at.warning)
    by_status = {r["status"]: r for r in recovery.list_replies()}
    assert by_status[ReplyStatus.TO_REVIEW]["proposed_action"] is ActionKind.HUMAN
    assert ReplyStatus.UNMATCHED in by_status


def test_confirming_a_promised_date_creates_the_hold(env, recovery):
    reply_id = add_reply(recovery)
    at = run_app("app_pages/reponses.py")
    at.button(key=f"confirmer-{reply_id}").click().run()
    assert not at.exception and not at.error
    assert recovery.active_holds(TODAY) == {KEY: date(2026, 10, 30)}
    assert recovery.get_reply(reply_id)["status"] is ReplyStatus.APPLIED


def test_dismissing_a_reply_changes_nothing_else(env, recovery):
    reply_id = add_reply(recovery)
    at = run_app("app_pages/reponses.py")
    at.button(key=f"classer-{reply_id}").click().run()
    assert recovery.get_reply(reply_id)["status"] is ReplyStatus.DISMISSED
    assert recovery.active_holds(TODAY) == {}


def test_imposing_five_installments_is_refused_with_the_reason(env, recovery):
    reply_id = add_reply(recovery, ReplyIntent.PLAN_REQUEST, ActionKind.PROPOSE_PLAN, None)
    at = run_app("app_pages/reponses.py")
    assert at.number_input(key=f"mensualites-{reply_id}").value == 4          # la proposition par défaut
    at.number_input(key=f"mensualites-{reply_id}").set_value(5).run()
    at.button(key=f"confirmer-{reply_id}").click().run()
    assert any("5 mensualités refusées" in e.value and "Trop de versements" in e.value for e in at.error)
    assert recovery.list_plans() == []
    assert recovery.get_reply(reply_id)["status"] is ReplyStatus.TO_REVIEW


def test_default_installments_create_a_proposed_plan(env, recovery):
    tmp_path, _ = env
    reply_id = add_reply(recovery, ReplyIntent.PLAN_REQUEST, ActionKind.PROPOSE_PLAN, None)
    at = run_app("app_pages/reponses.py")
    at.button(key=f"confirmer-{reply_id}").click().run()
    assert not at.exception and not at.error
    [plan] = recovery.list_plans()
    assert plan.status is PlanStatus.PROPOSED and len(plan.installments) == 4
    assert list((tmp_path / "deposees").glob("echeancier_*.eml"))


def test_reply_text_is_shown_verbatim_and_summary_escaped(env, recovery):
    add_reply(recovery, summary="**gras** [lien](http://evil.example)", body="<script>alert(1)</script> **brut**")
    at = run_app("app_pages/reponses.py")
    assert any(t.value == "<script>alert(1)</script> **brut**" for t in at.text)
    summaries = " ".join(m.value for m in at.markdown)
    assert md_escape("**gras** [lien](http://evil.example)") in summaries
    assert "[lien](http" not in summaries


def test_plans_page_shows_installments_and_remaining_then_marks_received(env, recovery):
    plan_id = active_plan(recovery)
    at = run_app("app_pages/echeanciers.py")
    assert [m.value for m in at.metric if m.label == "Reste dû"] == ["1 200,50 €"]
    first = recovery.get_plan(plan_id).installments[0]
    at.button(key=f"recu-{plan_id}-1").click().run()
    assert not at.exception
    assert recovery.get_plan(plan_id).installments[0].received_on == TODAY
    assert [m.value for m in at.metric if m.label == "Reste dû"] == [
        "{:,.2f} €".format(Decimal("1200.50") - first.amount).replace(",", " ").replace(".", ",")]


def test_cancelling_a_plan(env, recovery):
    plan_id = active_plan(recovery)
    at = run_app("app_pages/echeanciers.py")
    at.button(key=f"annuler-plan-{plan_id}").click().run()
    assert recovery.get_plan(plan_id).status is PlanStatus.CANCELLED


def test_plans_page_settles_a_plan_whose_invoices_are_paid(env, recovery):
    tmp_path, _ = env
    plan_id = active_plan(recovery)
    conn, repo = factures_conn(str(tmp_path / "factures.db"))
    repo.mark_paid(1, date(2026, 10, 10))
    conn.close()
    at = run_app("app_pages/echeanciers.py")
    assert recovery.get_plan(plan_id).status is PlanStatus.SETTLED
    assert any("soldé" in s.value for s in at.success)


def test_installment_reminder_is_validated_and_deposited(env, recovery):
    tmp_path, _ = env
    plan_id = active_plan(recovery)                           # 1re échéance le 05/10 : 10 jours de retard
    at = run_app("app_pages/echeanciers.py")
    key = f"valider-mensualite-{plan_id}-1-1"
    at.button(key=key).click().run()
    assert not at.exception and not at.error
    assert recovery.installment_reminders(plan_id) == {1: [(1, TODAY)]}
    [eml_file] = (tmp_path / "deposees").glob("echeancier_*_mensualite-1-palier-1.eml")
    assert f"[Réf. E-{plan_id:05d}]" in message_from_bytes(eml_file.read_bytes(), policy=policy.default)["Subject"]
    run_app("app_pages/echeanciers.py")                       # recharger : plus de relance due (délai de 7 jours)
    assert not any(b.key == key for b in at.button)


def test_only_the_earliest_installment_per_client_is_proposed(env, recovery):
    plan_id = active_plan(recovery, agreed=date(2026, 8, 1))  # échéances 31/08 et 30/09 : deux retards
    at = run_app("app_pages/echeanciers.py")
    keys = [b.key for b in at.button if str(b.key).startswith("valider-mensualite-")]
    assert keys == [f"valider-mensualite-{plan_id}-1-1"]


def test_client_on_hold_gets_no_installment_reminder(env, recovery):
    active_plan(recovery)
    recovery.add_hold(KEY, "suspension", None, "test", TODAY)
    at = run_app("app_pages/echeanciers.py")
    assert not [b for b in at.button if str(b.key).startswith("valider-mensualite-")]


def test_client_on_hold_gets_no_daily_reminder(env, recovery):
    _, store = env
    recovery.add_hold(KEY, "report", date(2026, 10, 30), "test", TODAY)
    prepare(run_app())
    assert pending(store) == []


def test_dispute_button_creates_the_dossier_and_shows_its_path(env, recovery):
    tmp_path, _ = env
    plan_id = active_plan(recovery, agreed=date(2026, 8, 1))
    for stage, when in ((1, date(2026, 9, 5)), (2, date(2026, 9, 20)), (3, date(2026, 10, 5))):
        recovery.record_installment_reminder(plan_id, 1, stage, when, None)
    at = run_app("app_pages/echeanciers.py")
    at.button(key=f"contentieux-{KEY}").click().run()
    assert not at.exception and not at.error
    [dispute] = recovery.list_disputes()
    assert Path(dispute["folder"]).parent == tmp_path / "dossiers" and Path(dispute["folder"]).is_dir()
    assert any(dispute["folder"] in c.value for c in at.code)
    assert recovery.get_plan(plan_id).status is PlanStatus.BROKEN
    page = run_app("app_pages/contentieux.py")
    assert any(dispute["folder"] in c.value for c in page.code)
    assert not any(str(b.key).startswith("contentieux-") for b in page.button)


def test_validated_reminders_show_their_reference_in_the_journal(env):
    _, store = env
    at = prepare(run_app())
    [reminder] = pending(store)
    next(b for b in at.button if b.key == f"valider-{reminder.id}").click().run()
    journal = run_app("app_pages/journal_validees.py")
    assert any(f"R-{reminder.id:05d}" in e.label for e in journal.expander)


def test_dashboard_shows_plans_and_disputes(env, recovery):
    active_plan(recovery)
    at = run_app("app_pages/tableau_de_bord.py")
    values = {m.label: m.value for m in at.metric}
    assert values["Échéanciers en cours"] == "1" and values["Reste dû sur échéanciers"] == "1 200,50 €"
    assert values["Contentieux"] == "0"


def add_unmatched(recovery, suspicious=False, message_id="<u1@x>", sender="compta@hotel-canuts.co") -> int:
    return recovery.add_reply(
        message_id=message_id, client_key=None, reminder_ref=None, match_method="aucune", sender=sender,
        subject="Re: [Réf. R-99999]", received_at="2026-10-14T09:00:00", body="Nouveau RIB.",
        intent=ReplyIntent.OTHER, promised_date=None, requested_installments=None,
        summary="Réponse non rattachée à un client.", suspicious=suspicious, proposed_action=ActionKind.NONE,
        action_until=None, status=ReplyStatus.UNMATCHED, cost_usd=0.0, today=TODAY)


def test_suspicious_unmatched_reply_shows_a_fraud_warning(env, recovery):
    add_unmatched(recovery, suspicious=True)
    at = run_app("app_pages/reponses.py")
    assert any("suspecte" in w.value for w in at.warning)
    assert any("Non rattachées" in s.value for s in at.subheader)


def test_clean_unmatched_reply_has_no_fraud_warning(env, recovery):
    add_unmatched(recovery)
    at = run_app("app_pages/reponses.py")
    assert not any("suspecte" in w.value for w in at.warning)


def test_client_agreement_by_hand_activates_a_proposed_plan(env, recovery):
    tmp_path, _ = env
    total = Decimal("1200.50")
    plan_id = recovery.create_plan(KEY, [1], total, plan_amounts(total, 4), TODAY)
    at = run_app("app_pages/echeanciers.py")
    at.button(key=f"accord-plan-{plan_id}").click().run()
    assert not at.exception and not at.error
    plan = recovery.get_plan(plan_id)
    assert plan.status is PlanStatus.ACTIVE and plan.agreed_on == TODAY
    assert (tmp_path / "deposees" / f"echeancier_{plan_id:05d}_confirmation.eml").exists()
    assert not any(b.key == f"accord-plan-{plan_id}" for b in at.button)


def test_releasing_a_hold_from_the_clients_page(env, recovery):
    hold_id = recovery.add_hold(KEY, "suspension", None, "Réponse 1 : contestation du client.", TODAY)
    at = run_app("app_pages/clients.py")
    assert any("contestation" in m.value for m in at.markdown)
    at.button(key=f"lever-pause-{hold_id}").click().run()
    assert not at.exception
    assert recovery.active_holds(TODAY) == {}
    assert not any(b.key == f"lever-pause-{hold_id}" for b in at.button)


def test_dispute_suspension_cannot_be_released(env, recovery):
    recovery.open_dispute_records(KEY, "dossier", Decimal("1200.50"), None, None, TODAY)
    at = run_app("app_pages/clients.py")
    assert not [b for b in at.button if str(b.key).startswith("lever-pause-")]
    assert any("contentieux" in c.value for c in at.caption)
