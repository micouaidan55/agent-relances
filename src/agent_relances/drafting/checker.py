"""Vérification par le code des faits d'un brouillon : rien de faux, rien d'oublié, rien d'inventé."""

import re
import unicodedata
from decimal import Decimal

from agent_relances.formatting import date_fr, euros
from agent_relances.models import DueReminder, ReminderDraft
from agent_relances.rules.legal import LEGAL_AMOUNT, LEGAL_MARKER

_SPACES = str.maketrans({" ": " ", " ": " ", " ": " "})
_AMOUNT = re.compile(r"(?<![\d,.])(\d{1,3}(?:[ .]\d{3})+|\d+)(?:,(\d{1,2}))?\s?(?:€|eur\b|euros?\b)", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
           "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}
_TEXT_DATE = re.compile(r"(?<!\d)(1er|\d{1,2})\s+(" + "|".join(_MONTHS) + r")\b")


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def amounts_in(text: str) -> set[Decimal]:
    found = set()
    for integer, cents in _AMOUNT.findall(text.translate(_SPACES)):
        digits = integer.replace(" ", "").replace(".", "")
        found.add(Decimal(f"{digits}.{(cents or '0').ljust(2, '0')}"))
    return found


def dates_in(text: str) -> set[tuple[int, int]]:
    found = set()
    for day, month, _ in _NUMERIC_DATE.findall(text):
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            found.add((int(day), int(month)))
    for day, month in _TEXT_DATE.findall(_plain(text)):
        found.add((1 if day == "1er" else int(day), _MONTHS[month]))
    return found


def check_draft(draft: ReminderDraft, due: DueReminder) -> list[str]:
    text = f"{draft.subject}\n{draft.body}".translate(_SPACES)
    problems = []
    for invoice in due.invoices:
        if invoice.number.casefold() not in text.casefold():
            problems.append(f"Numéro de facture absent : {invoice.number}")
    # Les numéros de facture peuvent contenir des chiffres et des « / » : on les retire avant d'analyser.
    cleaned = text
    for invoice in due.invoices:
        cleaned = re.sub(re.escape(invoice.number), " ", cleaned, flags=re.IGNORECASE)

    found_amounts = amounts_in(cleaned)
    expected_amounts = {invoice.amount for invoice in due.invoices}
    if len(due.invoices) > 1:
        expected_amounts.add(due.total)
    if due.legal_notice:
        expected_amounts.add(LEGAL_AMOUNT)
    problems += [f"Montant absent : {euros(a)}" for a in sorted(expected_amounts - found_amounts)]
    problems += [f"Montant non prévu : {euros(a)}" for a in sorted(found_amounts - expected_amounts)]

    found_dates = dates_in(cleaned)
    expected_dates = {(invoice.due_date.day, invoice.due_date.month) for invoice in due.invoices}
    for invoice in due.invoices:
        if (invoice.due_date.day, invoice.due_date.month) not in found_dates:
            problems.append(f"Échéance absente : {date_fr(invoice.due_date)} (facture {invoice.number})")
    problems += [f"Date non prévue : {d:02d}/{m:02d}" for d, m in sorted(found_dates - expected_dates)]

    has_notice = _plain(LEGAL_MARKER) in _plain(text)
    if due.legal_notice and not has_notice:
        problems.append("Mention légale de l'indemnité forfaitaire absente")
    if not due.legal_notice and has_notice:
        problems.append("Mention légale de l'indemnité forfaitaire non prévue pour ce palier ou ce client")
    return problems
