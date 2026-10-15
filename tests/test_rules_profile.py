from datetime import date, timedelta

import pytest

from agent_relances.models import PaymentRecord, Profile, Stage, Tone
from agent_relances.rules.legal import LEGAL_AMOUNT, LEGAL_MARKER, LEGAL_NOTICE, needs_legal_notice
from agent_relances.rules.profile import client_profile
from agent_relances.rules.tone import target_tone

TODAY = date(2026, 10, 15)


def history(*days_late, paid_days_ago=30):
    paid = TODAY - timedelta(days=paid_days_ago)
    return [PaymentRecord(customer="A", due_date=paid - timedelta(days=d), paid_at=paid) for d in days_late]


@pytest.mark.parametrize("lateness,expected", [
    ((0, 5, 10), Profile.GOOD),        # moyenne 5
    ((6,), Profile.OCCASIONAL),
    ((10, 30), Profile.OCCASIONAL),    # moyenne 20
    ((21,), Profile.CHRONIC),
])
def test_profile_by_mean_lateness(lateness, expected):
    assert client_profile(history(*lateness), overdue_count=1, today=TODAY) is expected


def test_no_history_is_occasional():
    assert client_profile([], overdue_count=1, today=TODAY) is Profile.OCCASIONAL


def test_two_overdue_invoices_make_a_chronic_late_payer():
    assert client_profile(history(0, 0), overdue_count=2, today=TODAY) is Profile.CHRONIC


def test_old_history_is_ignored():
    assert client_profile(history(40, paid_days_ago=400), overdue_count=1, today=TODAY) is Profile.OCCASIONAL


def test_early_payment_counts_as_on_time():
    early = [PaymentRecord(customer="A", due_date=TODAY - timedelta(days=10), paid_at=TODAY - timedelta(days=20))]
    assert client_profile(early, overdue_count=1, today=TODAY) is Profile.GOOD


@pytest.mark.parametrize("stage,profile,tone", [
    (Stage.COURTEOUS, Profile.GOOD, Tone.LIGHT), (Stage.COURTEOUS, Profile.OCCASIONAL, Tone.LIGHT),
    (Stage.COURTEOUS, Profile.CHRONIC, Tone.NEUTRAL), (Stage.FIRM, Profile.GOOD, Tone.NEUTRAL),
    (Stage.FIRM, Profile.OCCASIONAL, Tone.NEUTRAL), (Stage.FIRM, Profile.CHRONIC, Tone.FIRM),
    (Stage.FINAL, Profile.GOOD, Tone.FIRM), (Stage.FINAL, Profile.OCCASIONAL, Tone.FIRM),
    (Stage.FINAL, Profile.CHRONIC, Tone.FIRM),
])
def test_tone_table(stage, profile, tone):
    assert target_tone(stage, profile) is tone


@pytest.mark.parametrize("stage,professional,expected", [
    (Stage.FINAL, True, True), (Stage.FINAL, False, False),
    (Stage.FIRM, True, False), (Stage.COURTEOUS, True, False),
])
def test_legal_notice_only_for_professionals_at_the_last_stage(stage, professional, expected):
    assert needs_legal_notice(stage, professional) is expected


def test_legal_notice_text_mentions_the_amount_and_marker():
    assert "40 €" in LEGAL_NOTICE and LEGAL_MARKER in LEGAL_NOTICE and str(LEGAL_AMOUNT) == "40"
