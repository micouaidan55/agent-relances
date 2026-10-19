import unicodedata
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage

import pytest
from agent_emails.models import Email
from agent_emails.parsing import parse_eml

from agent_relances.models import Client, ReminderDraft
from agent_relances.replies.matching import MatchResult, fetch_replies, match_reply
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from tests.factories import client, due_reminder
from tests.test_recovery_store import add_reply

TODAY = date(2026, 10, 16)


def eml(subject="Re: Relance", sender="compta@hotel-canuts.fr", message_id="<m1@x>", body="Bonjour") -> bytes:
    message = EmailMessage()
    message["From"] = f"Compta <{sender}>"
    message["To"] = "relances@atelier.fr"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Thu, 15 Oct 2026 09:30:00 +0200"
    message.set_content(body)
    return message.as_bytes()


@pytest.fixture
def stores():
    conn = connect()
    reminders, recovery = ReminderStore(conn), RecoveryStore(conn)
    reminders.save_client(client())
    return reminders, recovery


def reminder_id(reminders) -> int:
    due = due_reminder()
    return reminders.add_validated(due, ReminderDraft(subject="Relance", body="Bonjour"), TODAY)


def test_reference_r_found_even_with_re_and_tr_prefixes(stores):
    reminders, recovery = stores
    rid = reminder_id(reminders)
    for prefix in ("Re: ", "TR: ", "RE: TR: "):
        email = parse_eml(eml(subject=f"{prefix}Relance facture [Réf. R-{rid:05d}]", sender="inconnu@autre.fr"))
        assert match_reply(email, reminders, recovery) == MatchResult("hotel des canuts", f"R-{rid:05d}", "reference")


def test_reference_e_gives_plan_client(stores):
    reminders, recovery = stores
    plan_id = recovery.create_plan("Hôtel des Canuts", [1], Decimal("300"), [Decimal("150"), Decimal("150")], TODAY)
    email = parse_eml(eml(subject=f"Re: Échéancier [Réf. E-{plan_id:05d}]", sender="inconnu@autre.fr"))
    assert match_reply(email, reminders, recovery) == MatchResult("hotel des canuts", f"E-{plan_id:05d}", "reference")


def test_unknown_reference_falls_back_to_sender(stores):
    reminders, recovery = stores
    email = parse_eml(eml(subject="Re: Relance [Réf. R-99999]"))
    assert match_reply(email, reminders, recovery) == MatchResult("hotel des canuts", None, "expediteur")


def test_sender_match_ignores_case_and_spaces(stores):
    reminders, recovery = stores
    reminders.save_client(Client(name="Café Rond", email="  Contact@Cafe-Rond.FR ", contact="", notes="", professional=True))
    email = parse_eml(eml(subject="Question", sender="CONTACT@cafe-rond.fr"))
    assert match_reply(email, reminders, recovery) == MatchResult("cafe rond", None, "expediteur")


def test_unmatched_reply(stores):
    reminders, recovery = stores
    email = parse_eml(eml(subject="Offre", sender="x@spam.io"))
    assert match_reply(email, reminders, recovery) == MatchResult(None, None, None)


def test_fetch_replies_keeps_only_matched_emails(tmp_path, stores):
    reminders, recovery = stores
    rid = reminder_id(reminders)
    (tmp_path / "a.eml").write_bytes(eml(subject=f"Re: [Réf. R-{rid:05d}]", sender="x@autre.fr", message_id="<a@x>"))
    (tmp_path / "b.eml").write_bytes(eml(subject="Bonjour", message_id="<b@x>"))
    (tmp_path / "c.eml").write_bytes(eml(subject="Pub", sender="x@spam.io", message_id="<c@x>"))
    found = fetch_replies(tmp_path, reminders, recovery)
    assert sorted(e.message_id for e, _ in found) == ["<a@x>", "<b@x>"]
    assert all(m.client_key == "hotel des canuts" for _, m in found)


def test_fetch_replies_skips_known_replies(tmp_path, stores):
    reminders, recovery = stores
    (tmp_path / "b.eml").write_bytes(eml(message_id="<b@x>"))
    (email, match), = fetch_replies(tmp_path, reminders, recovery)
    add_reply(recovery, message_id=email.message_id)
    assert fetch_replies(tmp_path, reminders, recovery) == []


def test_fetch_replies_missing_inbox_is_empty(tmp_path, stores):
    reminders, recovery = stores
    assert fetch_replies(tmp_path / "absent", reminders, recovery) == []


def test_fetch_replies_keeps_unknown_reference_from_unknown_sender_unattached(tmp_path, stores):
    reminders, recovery = stores
    (tmp_path / "d.eml").write_bytes(eml(subject="Re: [Réf. R-99999]", sender="x@autre.fr", message_id="<d@x>"))
    (email, match), = fetch_replies(tmp_path, reminders, recovery)
    assert match == MatchResult(None, None, None)


def test_empty_sender_never_matches_a_card_without_email(stores):
    reminders, recovery = stores
    reminders.save_client(Client(name="Sans Email", email="", contact="", notes="", professional=True))
    email = Email(message_id="<e@x>", sender_address="", subject="Bonjour", received_at=datetime(2026, 10, 15),
                  body="Bonjour")
    assert match_reply(email, reminders, recovery) == MatchResult(None, None, None)


def test_reference_wins_over_a_sender_of_another_client(stores):
    reminders, recovery = stores
    reminders.save_client(Client(name="Café Rond", email="contact@cafe-rond.fr", contact="", notes="",
                                 professional=True))
    rid = reminder_id(reminders)
    email = parse_eml(eml(subject=f"Re: Relance [Réf. R-{rid:05d}]", sender="contact@cafe-rond.fr"))
    assert match_reply(email, reminders, recovery) == MatchResult("hotel des canuts", f"R-{rid:05d}", "reference")


def test_first_reference_that_resolves_is_used(stores):
    reminders, recovery = stores
    rid = reminder_id(reminders)
    email = parse_eml(eml(subject=f"Re: [Réf. R-99999] Re: [Réf. R-{rid:05d}]", sender="x@autre.fr"))
    assert match_reply(email, reminders, recovery) == MatchResult("hotel des canuts", f"R-{rid:05d}", "reference")


def test_decomposed_accent_in_the_subject_still_matches(stores):
    reminders, recovery = stores
    rid = reminder_id(reminders)
    subject = unicodedata.normalize("NFD", f"Re: Relance [Réf. R-{rid:05d}]")
    assert "Réf" not in subject
    email = Email(message_id="<n@x>", sender_address="x@autre.fr", subject=subject,
                  received_at=datetime(2026, 10, 15), body="Bonjour")
    assert match_reply(email, reminders, recovery) == MatchResult("hotel des canuts", f"R-{rid:05d}", "reference")
