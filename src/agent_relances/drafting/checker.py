"""Vérification par le code des faits d'un brouillon : rien de faux, rien d'oublié, rien d'inventé."""

import re
import unicodedata
from collections.abc import Sequence
from datetime import date
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
# Nombre décimal à la française sans devise (« 9 999,99 »), compté comme un montant.
_BARE_AMOUNT = re.compile(r"(?<![\d.,])(?P<bare>(?:\d{1,3}(?:[ .]\d{3})+|\d+),\d{2})(?![\d])")
_K_AMOUNT = re.compile(rf"(?<!\d)(?P<k>{_NUMBER})\s?k\s?{_CURRENCY}", re.IGNORECASE)
_PERCENT = re.compile(r"\d+(?:[.,]\d+)?\s?%|(?<!\w)pour\s+cent(?!\w)", re.IGNORECASE)
_PARTS = re.compile(r"(?P<int>\d{1,3}(?:[ .,]\d{3})+|\d+)(?:[.,](?P<dec>\d+))?")

_SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])")
_DOTTED_DATE = re.compile(r"(?<![\d/.\-])(\d{1,2})([.\-])(\d{1,2})\2(\d{4}|\d{2})(?!\d|[/.\-]\d)")
_SHORT_DATE = re.compile(r"(?<![\d/.\-])(\d{1,2})([.\-])(\d{2})(?![\d/\-]|[.,]\d)")
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


def _strip_amounts(text: str) -> str:
    text = _K_AMOUNT.sub(" ", text)
    text = _AMOUNT.sub(" ", text)
    return _BARE_AMOUNT.sub(" ", text)


def amounts_in(text: str) -> set[Decimal]:
    text = text.translate(_SPACES)
    found = {_parse_amount(m["after"] or m["before"]) for m in _AMOUNT.finditer(text)}
    found |= {_parse_amount(m["k"]) * 1000 for m in _K_AMOUNT.finditer(text)}
    found |= {_parse_amount(m["bare"]) for m in _BARE_AMOUNT.finditer(_AMOUNT.sub(" ", _K_AMOUNT.sub(" ", text)))}
    return found


def dates_in(text: str) -> set[tuple[int, int, int | None]]:
    """Dates (jour, mois, année ou None) ; les montants sont retirés avant, pour ne pas lire « 12.05 € » comme une date."""
    text = _strip_amounts(text.translate(_SPACES))
    found = set()
    for m in _SLASH_DATE.finditer(text):
        found.add((int(m[1]), int(m[2]), _year(m[3]) if m[3] else None))
    for m in _DOTTED_DATE.finditer(text):
        found.add((int(m[1]), int(m[3]), _year(m[4])))
    for m in _SHORT_DATE.finditer(text):
        found.add((int(m[1]), int(m[3]), None))
    for m in _ISO_DATE.finditer(text):
        found.add((int(m[3]), int(m[2]), int(m[1])))
    for m in _TEXT_DATE.finditer(_plain(text)):
        found.add((1 if m[1].endswith("er") else int(m[1]), _MONTHS[m[2]], int(m[3]) if m[3] else None))
    return {(d, mo, y) for d, mo, y in found if 1 <= d <= 31 and 1 <= mo <= 12}


def _invoice_pattern(number: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(number)}(?![\w-])", re.IGNORECASE)


def _segments(text: str, due: DueReminder) -> dict[str, list[str]]:
    """Pour chaque facture, les bouts de texte qui suivent son numéro, jusqu'au prochain numéro ou à la fin de la ligne."""
    spots = sorted((m.start(), m.end(), invoice.number) for invoice in due.invoices
                   for m in _invoice_pattern(invoice.number).finditer(text))
    segments: dict[str, list[str]] = {invoice.number: [] for invoice in due.invoices}
    for index, (_, end, number) in enumerate(spots):
        stop = text.find("\n", end)
        stop = len(text) if stop == -1 else stop
        if index + 1 < len(spots):
            stop = min(stop, spots[index + 1][0])
        segments[number].append(text[end:stop])
    return segments


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
    segments = _segments(text, due)
    for invoice in due.invoices:
        if segments[invoice.number] and not any(
                invoice.amount in amounts_in(segment) and any(_matches(f, invoice) for f in dates_in(segment))
                for segment in segments[invoice.number]):
            problems.append(f"Montant ou échéance non rattaché à la facture {invoice.number}")
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

    problems += [f"Pourcentage non prévu : {m[0].strip()}" for m in _PERCENT.finditer(cleaned)]

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


def check_plan_draft(draft: ReminderDraft, amounts: list[Decimal], total: Decimal | None,
                     dates: list[date] | None, invoice_numbers: Sequence[str] = ()) -> list[str]:
    """Montants = exactement les mensualités et le total ; dates = exactement celles de l'échéancier, ou aucune."""
    text = f"{draft.subject}\n{draft.body}".translate(_SPACES)
    for number in invoice_numbers:   # les numéros de facture contiennent des chiffres et des « / » : on les retire
        text = _invoice_pattern(number).sub(" ", text)

    found_amounts = amounts_in(text)
    expected_amounts = set(amounts) | ({total} if total is not None else set())
    problems = [f"Montant absent : {_amount_label(a)}" for a in sorted(expected_amounts - found_amounts)]
    problems += [f"Montant non prévu : {_amount_label(a)}" for a in sorted(found_amounts - expected_amounts)]
    problems += [f"Pourcentage non prévu : {m[0].strip()}" for m in _PERCENT.finditer(text)]

    found_dates = dates_in(text)
    expected_dates = dates or []

    def planned(found: tuple[int, int, int | None]) -> bool:
        return any((found[0], found[1]) == (d.day, d.month) and found[2] in (None, d.year) for d in expected_dates)

    for d in expected_dates:
        if not any((f[0], f[1]) == (d.day, d.month) and f[2] in (None, d.year) for f in found_dates):
            problems.append(f"Échéance absente : {date_fr(d)}")
    for f in sorted(found_dates, key=lambda d: (d[2] or 0, d[1], d[0])):
        if not planned(f):
            problems.append(f"Date non prévue : {_date_label(f)}")
    return problems
