import re
import sqlite3
from datetime import date
from decimal import Decimal
from email import policy
from email.parser import BytesParser

import pytest
from pypdf import PdfReader

from agent_relances.disputes.dossier import build_dossier, open_dispute
from agent_relances.drafting.checker import amounts_in
from agent_relances.models import PlanStatus, ReminderDraft, ReminderStatus, ReplyIntent, ReplyStatus
from agent_relances.rules.installments import installment_steps
from agent_relances.rules.schedule import due_reminders
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, due_reminder, factures_conn, issued, overdue

D = Decimal
TODAY = date(2026, 12, 1)
KEY = "hotel des canuts"


def pdf_text(path) -> str:
    return " ".join(" ".join(page.extract_text().split()) for page in PdfReader(path).pages)


@pytest.fixture
def world(tmp_path):
    base = tmp_path / "factures"
    base.mkdir()
    (base / "AL-1.pdf").write_bytes(b"%PDF-1 un")
    conn, repo = factures_conn()
    id1 = repo.add(issued("AL-2026-101", amount="1200.50", due=date(2026, 9, 1)), "AL-1.pdf")
    id2 = repo.add(issued("AL-2026-102", amount="300.00", due=date(2026, 9, 15)), "absent.pdf")
    source = InvoiceSource(conn)
    reminders, recovery = ReminderStore(connect()), RecoveryStore(connect())
    reminders.save_client(client())
    return dict(base=base, out=tmp_path / "dossiers", source=source, reminders=reminders, recovery=recovery,
                ids=(id1, id2))


def validated(w, stage_day=date(2026, 10, 3), subject="Rappel facture AL-2026-101"):
    inv = [overdue(w["ids"][0], "AL-2026-101", amount="1200.50", due=date(2026, 9, 1), today=stage_day)]
    rid = w["reminders"].add_validated(due_reminder(invoices=inv, today=stage_day),
                                       ReminderDraft(subject=subject, body="Bonjour"), stage_day)
    return w["reminders"].get(rid)


def build(w, reminders=(), replies=(), plan=None, lawyer="avocat@cabinet.fr", out=None):
    invoices = w["source"].unpaid_of(KEY, TODAY)
    files = w["source"].files_of([i.invoice_id for i in invoices])
    return build_dossier(client(), invoices, files, list(reminders), list(replies), plan, out or w["out"], lawyer,
                         "Atelier Lumière SAS", TODAY, w["base"])


def test_files_of(world):
    files = world["source"].files_of(list(world["ids"]))
    assert files == {world["ids"][0]: "AL-1.pdf", world["ids"][1]: "absent.pdf"}
    assert world["source"].files_of([999]) == {}


def test_pdf_content_french(world):
    r1 = validated(world)
    r2 = validated(world, date(2026, 10, 20), "Deuxième relance")
    reply = dict(summary="Le client conteste la prestation", intent=ReplyIntent.DISPUTE,
                 received_at="2026-11-02T10:00:00", status=ReplyStatus.TO_REVIEW)
    result = build(world, [r1, r2], [reply])
    text = pdf_text(result.pdf_path)
    assert "Dossier de recouvrement" in text and "Hôtel des Canuts" in text and "Paul Girard" in text
    assert "AL-2026-101" in text and "AL-2026-102" in text
    assert "1 500,50 €" in text and result.remaining_due == D("1500.50")
    for day in ("03/10/2026", "20/10/2026"):
        assert day in text
    assert "Deuxième relance" in text and "conteste la prestation" in text
    assert result.folder == world["out"] / "hoteldescanuts_2026-12-01"


def test_only_validated_reminders_in_timeline(world):
    r = validated(world)
    pending = r.model_copy(update={"status": ReminderStatus.TO_VALIDATE, "decided_on": None, "subject": "Brouillon secret"})
    text = pdf_text(build(world, [r, pending]).pdf_path)
    assert "Rappel facture AL-2026-101" in text and "Brouillon secret" not in text


def test_remaining_after_received_installments(world):
    ids = list(world["ids"])
    pid = world["recovery"].create_plan(KEY, ids, D("1500.50"), [D("750.25"), D("750.25")], date(2026, 10, 1))
    world["recovery"].activate_plan(pid, date(2026, 10, 16))
    world["recovery"].mark_received(pid, 2, date(2026, 11, 20))
    plan = world["recovery"].get_plan(pid)
    result = build(world, plan=plan)
    text = pdf_text(result.pdf_path)
    assert result.remaining_due == D("750.25")
    assert "750,25 €" in text and "Reste dû" in text
    assert "reçue le 20/11/2026" in text and "reçue" in text.lower() and "manquée" in text.lower()
    assert "Échéancier (en cours)" in text and "en_cours" not in text


def test_files_copied_and_missing_listed(world):
    result = build(world)
    assert result.copied == ["AL-1.pdf"] and result.missing == ["absent.pdf"]
    assert (result.folder / "AL-1.pdf").read_bytes() == b"%PDF-1 un"
    assert not (result.folder / "absent.pdf").exists()
    assert "absent.pdf" in pdf_text(result.pdf_path)


def test_absolute_path_directory_and_name_clash(world, tmp_path):
    other = tmp_path / "ailleurs"
    other.mkdir()
    (other / "AL-1.pdf").write_bytes(b"autre")
    (world["base"] / "sub").mkdir()
    ids = world["ids"]
    files = {ids[0]: "AL-1.pdf", ids[1]: str(other / "AL-1.pdf")}
    invoices = world["source"].unpaid_of(KEY, TODAY)
    result = build_dossier(client(), invoices, files, [], [], None, world["out"], "", "X", TODAY, world["base"])
    assert sorted(result.copied) == ["AL-1.pdf", "AL-2026-102_AL-1.pdf"]
    assert (result.folder / "AL-1.pdf").read_bytes() == b"%PDF-1 un"
    assert (result.folder / "AL-2026-102_AL-1.pdf").read_bytes() == b"autre"
    # un dossier n'est pas un fichier à copier
    files = {ids[0]: "sub", ids[1]: "AL-1.pdf"}
    result = build_dossier(client(), invoices, files, [], [], None, world["out"], "", "X", TODAY, world["base"])
    assert result.missing == ["sub"]


def test_second_dossier_same_day_gets_suffix(world):
    first, second, third = build(world), build(world), build(world)
    assert first.folder.name == "hoteldescanuts_2026-12-01"
    assert second.folder.name.endswith("_2") and third.folder.name.endswith("_3")
    assert first.pdf_path.exists() and second.pdf_path.exists()


def test_eml_amounts_all_in_pdf_and_headers(world):
    result = build(world)
    msg = BytesParser(policy=policy.default).parsebytes(result.eml_path.read_bytes())
    assert msg["To"] == "avocat@cabinet.fr" and msg["X-Unsent"] == "1"
    assert msg["X-Agent-Relances"].startswith("contentieux-")
    body = msg.get_content()
    assert amounts_in(body) and amounts_in(body) <= amounts_in(pdf_text(result.pdf_path))
    assert str(result.folder) in body and not list(msg.iter_attachments())


def test_eml_without_lawyer(world):
    result = build(world, lawyer="")
    msg = BytesParser(policy=policy.default).parsebytes(result.eml_path.read_bytes())
    assert msg["To"] is None and "à compléter" in msg.get_content()


def test_euro_sign_survives_in_pdf(world):
    assert "€" in pdf_text(build(world).pdf_path)


def test_untrusted_text_cannot_break_pdf(world):
    reply = dict(summary="<b>gras <unclosed & </para>", intent=ReplyIntent.OTHER, received_at="2026-11-02",
                 status=ReplyStatus.APPLIED)
    assert "<b>gras" in pdf_text(build(world, replies=[reply]).pdf_path)


def dispute(w, **kw):
    return open_dispute(client(), w["recovery"], w["reminders"], w["source"], w["out"], "avocat@cabinet.fr",
                        "Atelier Lumière SAS", TODAY, w["base"], **kw)


def test_open_dispute_stops_everything(world):
    w = world
    ids = list(w["ids"])
    pid = w["recovery"].create_plan(KEY, ids, D("1500.50"), [D("750.25"), D("750.25")], date(2026, 10, 1))
    w["recovery"].activate_plan(pid, date(2026, 10, 16))
    plan = w["recovery"].get_plan(pid)
    overdue_ = w["source"].overdue(TODAY)
    clients = {KEY: client()}
    before = installment_steps(plan, {}, None, plan.installments[0].due_date.replace(day=20))
    assert before
    result = dispute(w)
    assert result.pdf_path.exists() and result.remaining_due == D("1500.50")
    assert w["recovery"].get_plan(pid).status is PlanStatus.BROKEN
    assert w["recovery"].active_holds(TODAY) == {KEY: None}
    [d] = w["recovery"].list_disputes()
    assert (d["client_key"], d["folder"], d["total_due"], d["created_on"]) == (KEY, str(result.folder), D("1500.50"), TODAY)
    assert any(e["action"] == "contentieux_ouvert" for e in w["reminders"].list_log())
    broken = w["recovery"].get_plan(pid)
    assert installment_steps(broken, {}, None, plan.installments[0].due_date.replace(day=20)) == []
    sched = due_reminders(overdue_, clients, {}, w["reminders"].invoice_states(), {}, TODAY,
                          holds=w["recovery"].active_holds(TODAY), planned_invoice_ids=w["recovery"].planned_invoice_ids())
    assert sched.reminders == [] and sched.to_humans == []


def test_dispute_without_plan_suspends_client(world):
    w = world
    clients = {KEY: client()}
    args = (w["source"].overdue(TODAY), clients, {}, w["reminders"].invoice_states(), {}, TODAY)
    assert due_reminders(*args, holds={}).reminders
    dispute(w)
    assert due_reminders(*args, holds=w["recovery"].active_holds(TODAY)).reminders == []


def test_second_dispute_refused(world):
    dispute(world)
    with pytest.raises(ValueError, match="contentieux"):
        dispute(world)
    assert len(world["recovery"].list_disputes()) == 1
    assert len(list(world["out"].iterdir())) == 1


def test_no_unpaid_invoice_refused(tmp_path):
    conn, _ = factures_conn()
    w = dict(recovery=RecoveryStore(connect()), reminders=ReminderStore(connect()), source=InvoiceSource(conn),
             out=tmp_path / "o", base=tmp_path)
    with pytest.raises(ValueError, match="impayée"):
        dispute(w)
    assert w["recovery"].list_disputes() == []


def custom_client(name):
    return client(name=name)


@pytest.mark.parametrize("name", ["Dupont/Fils", "../x", "..", "/"])
def test_folder_name_is_sanitized_inside_out_root(world, name):
    invoices = world["source"].unpaid_of(KEY, TODAY)
    result = build_dossier(custom_client(name), invoices, {}, [], [], None, world["out"], "", "X", TODAY, world["base"])
    assert result.folder.resolve().parent == world["out"].resolve()
    assert "/" not in result.folder.name and not result.folder.name.startswith(".")


def test_clash_name_is_sanitized(world, tmp_path):
    other = tmp_path / "ailleurs"
    other.mkdir()
    (other / "scan.pdf").write_bytes(b"deux")
    (world["base"] / "scan.pdf").write_bytes(b"un")
    conn, repo = factures_conn()
    a = repo.add(issued("2026/101", due=date(2026, 9, 1)), "x")
    b = repo.add(issued("../2026/102", due=date(2026, 9, 2)), "y")
    invoices = InvoiceSource(conn).unpaid_of(KEY, TODAY)
    files = {a: "scan.pdf", b: str(other / "scan.pdf")}
    result = build_dossier(client(), invoices, files, [], [], None, world["out"], "", "X", TODAY, world["base"])
    assert sorted(result.copied) == ["2026_101_scan.pdf", "scan.pdf"] or sorted(result.copied) == ["2026_102_scan.pdf", "scan.pdf"]
    assert {p.parent.resolve() for p in result.folder.iterdir()} == {result.folder.resolve()}


def test_reserved_names_are_case_insensitive(world):
    (world["base"] / "DOSSIER_RECOUVREMENT.PDF").write_bytes(b"x")
    invoices = world["source"].unpaid_of(KEY, TODAY)
    files = {world["ids"][0]: "DOSSIER_RECOUVREMENT.PDF"}
    result = build_dossier(client(), invoices, files, [], [], None, world["out"], "", "X", TODAY, world["base"])
    assert result.copied == ["AL-2026-101_DOSSIER_RECOUVREMENT.PDF"]
    assert PdfReader(result.pdf_path).pages


def test_remaining_with_covered_invoice_already_paid(world):
    w = world
    # facture couverte AL-101 payée dans agent-factures : seule AL-102 reste impayée et couverte
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.50"), [D("750.25"), D("750.25")], date(2026, 10, 1))
    w["recovery"].activate_plan(pid, date(2026, 10, 16))
    w["recovery"].mark_received(pid, 1, date(2026, 11, 15))
    plan = w["recovery"].get_plan(pid)
    invoices = [i for i in w["source"].unpaid_of(KEY, TODAY) if i.invoice_id == w["ids"][1]]  # 300,00
    result = build_dossier(client(), invoices, {}, [], [], plan, w["out"], "", "X", TODAY, w["base"])
    assert result.remaining_due == D("300.00")  # min(300, 750,25)


def test_remaining_with_uncovered_newer_invoice(world):
    w = world
    pid = w["recovery"].create_plan(KEY, [w["ids"][0]], D("1200.50"), [D("600.25"), D("600.25")], date(2026, 10, 1))
    w["recovery"].activate_plan(pid, date(2026, 10, 16))
    w["recovery"].mark_received(pid, 1, date(2026, 11, 15))
    plan = w["recovery"].get_plan(pid)
    result = build(w, plan=plan)  # AL-101 couverte (reste 600,25) + AL-102 hors échéancier (300,00)
    assert result.remaining_due == D("900.25")
    assert "900,25 €" in pdf_text(result.pdf_path)


def test_cancelled_plan_is_ignored_for_remaining(world):
    w = world
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.50"), [D("750.25")] * 2, date(2026, 10, 1))
    w["recovery"].set_plan_status(pid, PlanStatus.CANCELLED, TODAY)
    assert build(w, plan=w["recovery"].get_plan(pid)).remaining_due == D("1500.50")


def test_proposed_plan_is_cancelled(world):
    w = world
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.50"), [D("750.25")] * 2, date(2026, 10, 1))
    dispute(w)
    assert w["recovery"].get_plan(pid).status is PlanStatus.CANCELLED


def test_installment_reminders_in_timeline(world):
    w = world
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.50"), [D("750.25")] * 2, date(2026, 10, 1))
    w["recovery"].activate_plan(pid, date(2026, 10, 16))
    w["recovery"].record_installment_reminder(pid, 1, 1, date(2026, 11, 18), None)
    result = dispute(w)
    text = pdf_text(result.pdf_path)
    assert "18/11/2026" in text and "mensualité 1" in text


def test_dispute_failure_in_transaction_leaves_nothing(world):
    w = world
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.50"), [D("750.25")] * 2, date(2026, 10, 1))
    w["recovery"].activate_plan(pid, date(2026, 10, 16))
    w["recovery"]._conn.execute("DROP TABLE disputes")  # l'écriture du litige, dernière, échoue
    with pytest.raises(Exception):
        dispute(w)
    assert w["recovery"].active_holds(TODAY) == {}
    assert w["recovery"].get_plan(pid).status is PlanStatus.ACTIVE
    assert not w["out"].exists() or list(w["out"].iterdir()) == []


def test_dispute_failure_removes_folder_only(world, monkeypatch):
    w = world
    w["out"].mkdir()
    (w["out"] / "autre_dossier").mkdir()

    def boom(*a, **k):
        raise RuntimeError("panne")

    monkeypatch.setattr(RecoveryStore, "open_dispute_records", boom)
    with pytest.raises(RuntimeError):
        dispute(w)
    assert [p.name for p in w["out"].iterdir()] == ["autre_dossier"]
    assert w["recovery"].list_disputes() == [] and w["recovery"].active_holds(TODAY) == {}


def test_build_failure_removes_folder(world, monkeypatch):
    import agent_relances.disputes.dossier as mod

    def boom(*a, **k):
        raise RuntimeError("pdf")

    monkeypatch.setattr(mod, "_write_pdf", boom)
    with pytest.raises(RuntimeError):
        dispute(world)
    assert list(world["out"].iterdir()) == []


def test_log_failure_after_commit_keeps_the_dossier_and_the_dispute(world, monkeypatch):
    w = world

    def boom(*a, **k):
        raise sqlite3.OperationalError("journal verrouillé")

    monkeypatch.setattr(w["reminders"], "log", boom)
    result = dispute(w)                                       # le contentieux est enregistré : pas d'exception
    assert result.folder.is_dir() and result.pdf_path.exists()
    [d] = w["recovery"].list_disputes()
    assert d["folder"] == str(result.folder)
    assert w["recovery"].active_holds(TODAY) == {KEY: None}


def test_pdf_mentions_the_derogation_and_its_reason(world):
    w = world
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.00"), [D("250.00")] * 6, date(2026, 10, 1),
                                    exception_reason="Client en difficulté, accord de la direction")
    w["recovery"].activate_plan(pid, date(2026, 10, 16))
    text = pdf_text(build(w, plan=w["recovery"].get_plan(pid)).pdf_path)
    assert "dérogation" in text.lower() and "6 mensualités" in text
    assert "Client en difficulté, accord de la direction" in text


def test_pdf_without_derogation_does_not_mention_one(world):
    w = world
    pid = w["recovery"].create_plan(KEY, list(w["ids"]), D("1500.50"), [D("750.25")] * 2, date(2026, 10, 1))
    text = pdf_text(build(w, plan=w["recovery"].get_plan(pid)).pdf_path)
    assert "érogation" not in text
