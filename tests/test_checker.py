from datetime import date
from decimal import Decimal

import pytest

from agent_relances.drafting.checker import amounts_in, check_draft, dates_in
from agent_relances.drafting.fallback import fallback_draft
from agent_relances.models import ReminderDraft, Stage, Tone
from agent_relances.rules.legal import LEGAL_NOTICE
from tests.factories import due_reminder, overdue

ONE = due_reminder(invoices=[overdue(1, "AL-2026-101", amount="1200.50", due=date(2026, 10, 1))])
TWO = due_reminder(invoices=[overdue(1, "AL-2026-101", amount="1200.50", due=date(2026, 10, 1)),
                             overdue(2, "AL-2026-107", amount="99.50", due=date(2026, 10, 5))])
GOOD = ("Bonjour Paul, sauf oubli, la facture AL-2026-101 de 1 200,50 € TTC, échue le 01/10/2026, "
        "n'est pas encore réglée. Pourriez-vous nous indiquer la date de règlement prévue ? Atelier Lumière SAS")


def draft(body, subject="Rappel facture AL-2026-101"):
    return ReminderDraft(subject=subject, body=body)


@pytest.mark.parametrize("text,expected", [
    ("1 200,50 €", {Decimal("1200.50")}),
    ("1 200,50 €", {Decimal("1200.50")}),
    ("1 200,50 €", {Decimal("1200.50")}),
    ("1.200,50 €", {Decimal("1200.50")}),
    ("1200,5 €", {Decimal("1200.50")}),
    ("1 200.50 €", {Decimal("1200.50")}),
    ("1,250.50 €", {Decimal("1250.50")}),
    ("1.200 €", {Decimal("1200")}),
    ("1200.50 €", {Decimal("1200.50")}),
    ("30.50 €", {Decimal("30.50")}),
    ("1250,505 €", {Decimal("1250.505")}),
    ("€ 300", {Decimal("300")}),
    ("EUR 300", {Decimal("300")}),
    ("40 euros et 12 480,00 EUR", {Decimal("40"), Decimal("12480")}),
    ("le 15 octobre, 3 factures", set()),
])
def test_amounts_in(text, expected):
    assert amounts_in(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("échue le 01/10/2026", {(1, 10, 2026)}),
    ("le 15/10", {(15, 10, None)}),
    ("au 1er octobre 2026 et au 5 décembre", {(1, 10, 2026), (5, 12, None)}),
    ("facture 2026/101", set()),
    ("22-10-2026", {(22, 10, 2026)}),
    ("22.10.2026", {(22, 10, 2026)}),
    ("2026-10-22", {(22, 10, 2026)}),
    ("22 oct.", {(22, 10, None)}),
    ("1 er octobre", {(1, 10, None)}),
    ("12.05 €", set()),
])
def test_dates_in(text, expected):
    assert dates_in(text) == expected


def test_exact_draft_passes():
    assert check_draft(draft(GOOD), ONE) == []


def test_equivalent_amount_formats_pass():
    assert check_draft(draft(GOOD.replace("1 200,50 €", "1200,50 €")), ONE) == []
    assert check_draft(draft(GOOD.replace("1 200,50 €", "1 200.50 €")), ONE) == []
    assert check_draft(draft(GOOD.replace("1 200,50 €", "1 200,50 €")), ONE) == []


def test_equivalent_date_format_passes():
    assert check_draft(draft(GOOD.replace("01/10/2026", "1 er octobre 2026")), ONE) == []


@pytest.mark.parametrize("body,problem", [
    (GOOD.replace("1 200,50 €", "1 250,50 €"), "Montant absent : 1 200,50 €"),
    (GOOD.replace("AL-2026-101", "AL-2026-110"), "Numéro de facture absent : AL-2026-101"),
    (GOOD.replace("01/10/2026", "1er septembre 2026"), "Échéance absente : 01/10/2026"),
    (GOOD.replace("01/10/2026", "01/10/2025"), "Échéance absente : 01/10/2026"),
    (GOOD.replace("01/10/2026", "01/10/2025"), "Date non prévue : 01/10/2025"),
    (GOOD + " Merci de régler avant le 22/10/2026.", "Date non prévue : 22/10/2026"),
    (GOOD + " Merci de régler avant le 22-10-2026.", "Date non prévue : 22/10/2026"),
    (GOOD + " Merci de régler avant le 22.10.2026.", "Date non prévue : 22/10/2026"),
    (GOOD + " Merci de régler avant le 2026-10-22.", "Date non prévue : 22/10/2026"),
    (GOOD + " Merci de régler avant le 22 oct.", "Date non prévue : 22/10"),
    (GOOD + " Un escompte de 30 € est possible.", "Montant non prévu : 30,00 €"),
    (GOOD + " Frais : 30.50 €.", "Montant non prévu : 30,50 €"),
    (GOOD + " Frais : 1,250.50 €.", "Montant non prévu : 1 250,50 €"),
    (GOOD + " Frais : € 300.", "Montant non prévu : 300,00 €"),
    (GOOD + " Frais : EUR 300.", "Montant non prévu : 300,00 €"),
    (GOOD + " Frais : 1250,505 €.", "Montant non prévu : 1 250,505 €"),
    (GOOD + " " + LEGAL_NOTICE, "Mention légale de l'indemnité forfaitaire non prévue"),
    (GOOD.replace("AL-2026-101", "AL-2026-1010"), "Numéro de facture absent : AL-2026-101"),
])
def test_wrong_facts_are_reported(body, problem):
    # Objet sans numéro de facture : sinon le numéro absent du corps serait trouvé dans l'objet.
    assert any(p.startswith(problem) for p in check_draft(draft(body, subject="Rappel de facture"), ONE))


def test_grouped_reminder_needs_the_total():
    body = ("Factures AL-2026-101 (1 200,50 €, échue le 01/10/2026) et AL-2026-107 (99,50 €, échue le 05/10/2026). "
            "Merci de nous indiquer la date de règlement.")
    assert check_draft(draft(body), TWO) == ["Montant absent : 1 300,00 €"]
    assert check_draft(draft(body + " Total dû : 1 300,00 €."), TWO) == []


def test_legal_notice_required_at_the_last_stage_for_professionals():
    final = due_reminder(invoices=ONE.invoices, stage=Stage.FINAL, tone=Tone.FIRM, legal=True)
    assert "Mention légale de l'indemnité forfaitaire absente" in check_draft(draft(GOOD), final)
    assert check_draft(draft(GOOD + " " + LEGAL_NOTICE), final) == []


@pytest.mark.parametrize("stage,tone,legal", [
    (Stage.COURTEOUS, Tone.LIGHT, False), (Stage.FIRM, Tone.NEUTRAL, False),
    (Stage.FINAL, Tone.FIRM, True), (Stage.FINAL, Tone.FIRM, False),
])
@pytest.mark.parametrize("invoices", [ONE.invoices, TWO.invoices])
def test_fallback_always_passes_the_check(stage, tone, legal, invoices):
    due = due_reminder(invoices=invoices, stage=stage, tone=tone, legal=legal)
    result = fallback_draft(due, "Atelier Lumière SAS")
    assert check_draft(result, due) == []
    assert result.body.rstrip().endswith("Atelier Lumière SAS") and "Paul Girard" in result.body
