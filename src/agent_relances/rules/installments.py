"""Règle d'échéancier : calculée et vérifiée uniquement par le code, jamais par le modèle."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from agent_relances.models import Plan, PlanStatus, Stage
from agent_relances.rules.schedule import MIN_GAP_DAYS, StageDelays

MAX_INSTALLMENTS = 4
EXCEPTION_MAX_INSTALLMENTS = 12   # dérogation humaine motivée uniquement
MIN_INSTALLMENT = Decimal("100")
SMALL_TOTAL = Decimal("200")
FIRST_DUE_DAYS = 30

_CENT = Decimal("0.01")


class PlanRuleError(ValueError):
    """Demande d'échéancier impossible à satisfaire dans la règle de l'entreprise."""


def max_installments(total: Decimal, *, exception: bool = False) -> int:
    """Plafond du nombre de versements ; `exception` (dérogation humaine motivée) le relève de 4 à 12, le minimum
    de 100 € par versement restant obligatoire."""
    if total < SMALL_TOTAL:
        return 2
    cap = EXCEPTION_MAX_INSTALLMENTS if exception else MAX_INSTALLMENTS
    return min(cap, int(total // MIN_INSTALLMENT))


def plan_amounts(total: Decimal, requested: int | None, *, exception: bool = False) -> list[Decimal]:
    if total != total.quantize(_CENT):
        raise PlanRuleError("Le total doit être exprimé en centimes entiers.")
    if total < 2 * _CENT:
        raise PlanRuleError("Le total est trop faible pour être réparti en au moins 2 versements.")
    if requested == 1:
        raise PlanRuleError("Un paiement en une seule fois n'est pas un échéancier.")
    maximum = max_installments(total, exception=exception)
    count = requested if requested is not None and 2 <= requested <= maximum else maximum
    base = (total / count).quantize(_CENT, rounding=ROUND_DOWN)
    amounts = [base] * (count - 1)
    amounts.append(total - base * (count - 1))
    return amounts


def plan_violations(amounts: list[Decimal], total: Decimal, *, exception: bool = False) -> list[str]:
    problems: list[str] = []
    if len(amounts) < 2:
        problems.append("Un échéancier compte au moins 2 versements.")
    maximum = max_installments(total, exception=exception)
    if len(amounts) > maximum:
        problems.append(f"Trop de versements : {len(amounts)} pour un maximum de {maximum}.")
    if any(a != a.quantize(_CENT) for a in amounts):
        problems.append("Les montants doivent être en centimes entiers.")
    if any(a < _CENT for a in amounts):
        problems.append("Chaque versement doit être strictement positif.")
    if sum(amounts, Decimal("0")) != total:
        problems.append("La somme des versements ne correspond pas au total dû.")
    if total >= SMALL_TOTAL and any(a < MIN_INSTALLMENT for a in amounts):
        problems.append("Chaque versement doit être d'au moins 100 €.")
    return problems


def installment_dates(agreed_on: date, count: int) -> list[date]:
    first = agreed_on + timedelta(days=FIRST_DUE_DAYS)
    dates = [first]
    for k in range(1, count):
        month_index = first.month - 1 + k
        year, month = first.year + month_index // 12, month_index % 12 + 1
        day = min(first.day, calendar.monthrange(year, month)[1])
        dates.append(date(year, month, day))
    return dates


@dataclass(frozen=True)
class InstallmentDue:
    plan_id: int
    number: int
    amount: Decimal
    due_date: date
    stage: Stage
    days_late: int


def installment_steps(
    plan: Plan, done: dict[int, list[tuple[int, date]]], last_contact: date | None, today: date,
    delays: StageDelays = StageDelays(),
) -> list[InstallmentDue]:
    """Relances de mensualités dues aujourd'hui : un palier à la fois, le suivant seulement une fois le précédent
    validé et son délai atteint, et pas avant MIN_GAP_DAYS jours après le dernier contact avec le client.

    Renvoie TOUTES les mensualités dues de ce plan, sans regroupement : c'est à l'appelant de n'en garder qu'une par
    client et par jour (la plus ancienne, voir web._due_installments), de sauter les clients en pause, et de passer
    en `last_contact` le dernier contact fusionné (relances et relances de mensualités, plans.merged_last_contacts)."""
    if plan.status is not PlanStatus.ACTIVE:
        return []
    if last_contact is not None and (today - last_contact).days < MIN_GAP_DAYS:
        return []
    result = []
    for installment in sorted(plan.installments, key=lambda i: i.number):
        if installment.received_on is not None or installment.due_date is None:
            continue
        days_late = (today - installment.due_date).days
        last_stage = max((stage for stage, _ in done.get(installment.number, [])), default=0)
        if last_stage >= Stage.FINAL:
            continue
        stage = Stage(last_stage + 1)
        if days_late >= delays.for_stage(stage):
            result.append(InstallmentDue(plan.id, installment.number, installment.amount, installment.due_date,
                                         stage, days_late))
    return result


def dispute_due(plan: Plan, done: dict[int, list[tuple[int, date]]], today: date) -> bool:
    """Une mensualité non reçue a ses 3 relances validées, la dernière date d'au moins MIN_GAP_DAYS jours."""
    if plan.status is not PlanStatus.ACTIVE:
        return False
    for installment in plan.installments:
        reminders = done.get(installment.number, [])
        if installment.received_on is None and {stage for stage, _ in reminders} >= {1, 2, 3}:
            if (today - max(decided for _, decided in reminders)).days >= MIN_GAP_DAYS:
                return True
    return False
