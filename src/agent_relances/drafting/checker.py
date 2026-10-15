"""Vérification par le code des faits d'un brouillon : rien de faux, rien d'oublié, rien d'inventé."""

import re
import unicodedata
from decimal import Decimal

from agent_relances.formatting import date_fr
from agent_relances.models import DueReminder, ReminderDraft
from agent_relances.rules.legal import LEGAL_AMOUNT, LEGAL_MARKER

_SPACES = str.maketrans({" ": " ", " ": " ", " ": " "})

# Montant : groupes de milliers séparés par espace, point ou virgule, ou entier simple, avec décimale optionnelle.
_NUMBER = r"(?:\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
_CURRENCY = r"(?:€|(?<!\w)eur(?!\w)|(?<!\w)euros?(?!\w))"
_AMOUNT = re.compile(
    rf"(?<!\d)(?:(?P<after>{_NUMBER})\s?{_CURRENCY}|{_CURRENCY}\s?(?P<before>{_NUMBER}))", re.IGNORECASE)
_PARTS = re.compile(r"(?P<int>\d{1,3}(?:[ .,]\d{3})+|\d+)(?:[.,](?P<dec>\d+))?")

_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])")
_DOTTED_DATE = re.compile(r"(?<![\d/.\-])(\d{1,2})([.\-])(\d{1,2})\2(\d{4}|\d{2})(?!\d|[/.\-]\d)")
_ISO_DATE = re.compile(r"(?<![\d/.\-])(\d{4})-(\d{1,2})-(\d{1,2})(?!\d|[/.\-]\d)")
_MONTHS = {"janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "mars": 3, "avril": 4, "avr": 4, "mai": 5,
           "juin": 6, "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "sept": 9, "octobre": 10, "oct": 10,
           "novembre": 11, "nov": 11, "decembre": 12, "dec": 12}
_TEXT_DATE = re.compile(
    rf"(?<!\d)(1\s?er|\d{{1,2}})\s+({'|'.join(sorted(_MONTHS, key=len, reverse=True))})(?![a-z])\.?(?:\s+(\d{{4}}))?")


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


def _parse_amount(token: str) -> Decimal:
    """Le dernier séparateur est la décimale s'il n'a pas la forme d'un groupe de milliers (1.200 ou 1,250.50)."""
    match = _PARTS.fullmatch(token)
    integer = re.sub(r"[ .,]", "", match["int"])
    return Decimal(f"{integer}.{match['dec'] or '0'}")


def _year(value: str) -> int:
    return int(value) + 2000 if len(value) == 2 else int(value)


def amounts_in(text: str) -> set[Decimal]:
    return {_parse_amount(m["after"] or m["before"]) for m in _AMOUNT.finditer(text.translate(_SPACES))}


def dates_in(text: str) -> set[tuple[int, int, int | None]]:
    """Dates (jour, mois, année ou None) ; les montants sont retirés avant, pour ne pas lire « 12.05 € » comme une date."""
    text = _AMOUNT.sub(" ", text.translate(_SPACES))
    found = set()
    for m in _SLASH_DATE.finditer(text):
        found.add((int(m[1]), int(m[2]), _year(m[3]) if m[3] else None))
    for m in _DOTTED_DATE.finditer(text):
        found.add((int(m[1]), int(m[3]), _year(m[4])))
    for m in _ISO_DATE.finditer(text):
        found.add((int(m[3]), int(m[2]), int(m[1])))
    for m in _TEXT_DATE.finditer(_plain(text)):
        found.add((1 if m[1].endswith("er") else int(m[1]), _MONTHS[m[2]], int(m[3]) if m[3] else None))
    return {(d, mo, y) for d, mo, y in found if 1 <= d <= 31 and 1 <= mo <= 12}


def _invoice_pattern(number: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(number)}(?![\w-])", re.IGNORECASE)


def _amount_label(amount: Decimal) -> str:
    digits = max(2, -amount.as_tuple().exponent)
    return f"{amount:,.{digits}f} €".replace(",", " ").replace(".", ",")


def _date_label(found: tuple[int, int, int | None]) -> str:
    day, month, year = found
    return f"{day:02d}/{month:02d}" if year is None else f"{day:02d}/{month:02d}/{year}"


def _matches(found: tuple[int, int, int | None], invoice) -> bool:
    day, month, year = found
    due = invoice.due_date
    return (day, month) == (due.day, due.month) and year in (None, due.year)


def check_draft(draft: ReminderDraft, due: DueReminder) -> list[str]:
    text = f"{draft.subject}\n{draft.body}".translate(_SPACES)
    problems = []
    for invoice in due.invoices:
        if not _invoice_pattern(invoice.number).search(text):
            problems.append(f"Numéro de facture absent : {invoice.number}")
    # Les numéros de facture peuvent contenir des chiffres et des « / » : on les retire avant d'analyser.
    cleaned = text
    for invoice in due.invoices:
        cleaned = _invoice_pattern(invoice.number).sub(" ", cleaned)

    found_amounts = amounts_in(cleaned)
    expected_amounts = {invoice.amount for invoice in due.invoices}
    if len(due.invoices) > 1:
        expected_amounts.add(due.total)
    if due.legal_notice:
        expected_amounts.add(LEGAL_AMOUNT)
    problems += [f"Montant absent : {_amount_label(a)}" for a in sorted(expected_amounts - found_amounts)]
    problems += [f"Montant non prévu : {_amount_label(a)}" for a in sorted(found_amounts - expected_amounts)]

    found_dates = dates_in(cleaned)
    for invoice in due.invoices:
        if not any(_matches(f, invoice) for f in found_dates):
            problems.append(f"Échéance absente : {date_fr(invoice.due_date)} (facture {invoice.number})")
    for f in sorted(found_dates, key=lambda d: (d[2] or 0, d[1], d[0])):
        if not any(_matches(f, invoice) for invoice in due.invoices):
            problems.append(f"Date non prévue : {_date_label(f)}")

    has_notice = _plain(LEGAL_MARKER) in _plain(text)
    if due.legal_notice and not has_notice:
        problems.append("Mention légale de l'indemnité forfaitaire absente")
    if not due.legal_notice and has_notice:
        problems.append("Mention légale de l'indemnité forfaitaire non prévue pour ce palier ou ce client")
    return problems
