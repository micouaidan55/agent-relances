"""Aller-retour complet : proposition déposée, réponse « Re: » d'une autre adresse, accord confirmé, mensualité
manquée et ses trois relances, contentieux proposé puis ouvert."""

from datetime import date, timedelta
from decimal import Decimal
from email import message_from_bytes, policy
from email.message import EmailMessage

from agent_relances.disputes.dossier import open_dispute
from agent_relances.models import ActionKind, PlanStatus, ReplyIntent, ReplyReading, ReplyStatus
from agent_relances.plans import merged_last_contacts
from agent_relances.replies.process import confirm_reply, process_replies
from agent_relances.rules.installments import dispute_due, installment_steps
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, factures_conn, issued
from tests.fakes import FakeClient, reply

TODAY = date(2026, 10, 16)
KEY = "hotel des canuts"
COMPANY = "Atelier Lumière"


def write_eml(folder, name, subject, sender, body):
    message = EmailMessage()
    message["From"] = f"Direction <{sender}>"
    message["To"] = "relances@atelier.fr"
    message["Subject"] = subject
    message["Message-ID"] = f"<{name}@x>"
    message["Date"] = "Fri, 16 Oct 2026 09:30:00 +0200"
    message.set_content(body)
    (folder / f"{name}.eml").write_bytes(message.as_bytes())


def test_round_trip_from_proposal_to_dispute(tmp_path):
    conn, repo = factures_conn()
    repo.add(issued("AL-1", amount="300.00", due=date(2026, 9, 1)), "1.pdf")
    repo.add(issued("AL-2", amount="500.00", due=date(2026, 9, 15)), "2.pdf")
    source = InvoiceSource(conn)
    db = connect()
    reminders, recovery = ReminderStore(db), RecoveryStore(db)
    reminders.save_client(client())
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()

    def collect(reading: ReplyReading) -> int:
        [reply_id] = process_replies(inbox, reminders, recovery, source, FakeClient([reply(reading)]), COMPANY,
                                     "atelier.fr", TODAY)
        return reply_id

    # 1. Demande d'échéancier depuis l'adresse de la fiche : la proposition est déposée.
    write_eml(inbox, "demande", "Re: Relance", "compta@hotel-canuts.fr", "Pouvons-nous payer en plusieurs fois ?")
    request = collect(ReplyReading(intent=ReplyIntent.PLAN_REQUEST, summary="Demande."))
    assert confirm_reply(request, reminders, recovery, source, outbox, None, COMPANY, TODAY) == []
    plan = recovery.open_plan_for(KEY)
    [proposal] = outbox.glob("*_proposition.eml")
    subject = message_from_bytes(proposal.read_bytes(), policy=policy.default)["Subject"]
    assert f"[Réf. E-{plan.id:05d}]" in subject

    # 2. Le client répond « Re: <objet> » depuis une autre adresse : rattaché par la référence.
    write_eml(inbox, "accord", f"Re: {subject}", "direction@groupe-hotelier.fr", "Nous acceptons.")
    agreement = collect(ReplyReading(intent=ReplyIntent.PLAN_AGREEMENT, summary="Accord."))
    stored = recovery.get_reply(agreement)
    assert (stored["client_key"], stored["match_method"], stored["reminder_ref"]) == (
        KEY, "reference", f"E-{plan.id:05d}")
    assert stored["suspicious"] is False and stored["proposed_action"] is ActionKind.ACTIVATE_PLAN

    # 3. L'humain confirme : l'échéancier est en cours, première échéance 30 jours après l'accord.
    assert confirm_reply(agreement, reminders, recovery, source, outbox, None, COMPANY, TODAY) == []
    assert recovery.get_reply(agreement)["status"] is ReplyStatus.APPLIED
    plan = recovery.get_plan(plan.id)
    assert plan.status is PlanStatus.ACTIVE and plan.total == Decimal("800.00")
    first_due = plan.installments[0].due_date
    assert first_due == TODAY + timedelta(days=30)

    # 4. Mensualité 1 manquée : trois relances validées (J+3, J+15, J+30), chacune due ce jour-là seulement.
    for days, stage in ((3, 1), (15, 2), (30, 3)):
        on = first_due + timedelta(days=days)
        assert not dispute_due(plan, recovery.installment_reminders(plan.id), on)
        dues = installment_steps(plan, recovery.installment_reminders(plan.id),
                                 merged_last_contacts(reminders, recovery).get(KEY), on)
        due = min(dues, key=lambda d: d.number)
        assert (due.number, int(due.stage)) == (1, stage)
        assert recovery.record_installment_reminder(plan.id, 1, stage, on, None)

    # 5. Sept jours après la 3e relance, sans paiement : contentieux proposé, puis ouvert.
    third = first_due + timedelta(days=30)
    assert not dispute_due(plan, recovery.installment_reminders(plan.id), third + timedelta(days=6))
    on = third + timedelta(days=7)
    assert dispute_due(plan, recovery.installment_reminders(plan.id), on)
    result = open_dispute(client(), recovery, reminders, source, tmp_path / "dossiers", "avocat@cabinet.fr",
                          COMPANY, on, tmp_path)
    assert result.pdf_path.exists() and result.eml_path.exists()
    assert result.remaining_due == Decimal("800.00")
    assert recovery.get_plan(plan.id).status is PlanStatus.BROKEN
    assert recovery.active_holds(on) == {KEY: None}
    assert [d["client_key"] for d in recovery.list_disputes()] == [KEY]
