from datetime import date
from decimal import Decimal

import pytest

from agent_relances.rules.installments import (
    PlanRuleError,
    installment_dates,
    max_installments,
    plan_amounts,
    plan_violations,
)


@pytest.mark.parametrize(
    "total,requested,expected",
    [
        ("1200", None, ["300.00"] * 4),
        ("1200", 2, ["600.00"] * 2),
        ("250", None, ["125.00"] * 2),
        ("150", None, ["75.00"] * 2),
        ("150", 4, ["75.00"] * 2),
        ("450", 6, ["112.50"] * 4),
        ("399.99", None, ["133.33", "133.33", "133.33"]),
        ("1000", 3, ["333.33", "333.33", "333.34"]),
    ],
)
def test_plan_amounts(total, requested, expected):
    assert plan_amounts(Decimal(total), requested) == [Decimal(e) for e in expected]


def test_one_payment_is_not_a_plan():
    with pytest.raises(PlanRuleError):
        plan_amounts(Decimal("500"), 1)


def test_total_below_one_cent_pair_is_refused():
    with pytest.raises(PlanRuleError):
        plan_amounts(Decimal("0.01"), None)


def test_max_installments():
    assert [max_installments(Decimal(t)) for t in ("199.99", "200", "299.99", "300", "400", "20000")] == [
        2, 2, 2, 3, 4, 4,
    ]


def _assert_rule(total: Decimal, requested: int | None) -> None:
    amounts = plan_amounts(total, requested)
    assert plan_violations(amounts, total) == [], (total, requested)
    assert sum(amounts) == total, (total, requested)
    maximum = max_installments(total)
    chosen = requested if requested is not None and 2 <= requested <= maximum else maximum
    assert len(amounts) == chosen, (total, requested)   # le nombre demandé s'il est valide, sinon le maximum


def test_rule_holds_for_every_amount():
    cent = Decimal("0.01")
    all_requests = [None, *range(2, 13)]
    n = 0
    for i in range(2, 100_001):
        total = Decimal(i) * cent
        for requested in all_requests:
            _assert_rule(total, requested)
            n += 1
    for euros in range(1001, 20_001):
        total = Decimal(euros)
        for requested in all_requests:
            _assert_rule(total, requested)
            n += 1
    assert n == 99_999 * 12 + 19_000 * 12 == 1_427_988  # nombre cité dans le README


@pytest.mark.parametrize(
    "amounts,total",
    [
        (["100", "100", "100", "100", "100"], "500"),
        (["200", "50"], "250"),
        (["90", "90", "90"], "270"),
        (["600"], "600"),
        (["100", "100"], "250"),
        (["100.005", "99.995"], "200"),
    ],
)
def test_violations_detected(amounts, total):
    assert plan_violations([Decimal(a) for a in amounts], Decimal(total))


def test_installment_dates():
    assert installment_dates(date(2026, 10, 16), 4) == [
        date(2026, 11, 15), date(2026, 12, 15), date(2027, 1, 15), date(2027, 2, 15),
    ]
    assert installment_dates(date(2026, 12, 1), 3) == [
        date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 28),
    ]


def test_readme_cites_the_true_number_of_exhaustive_cases():
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert f"{99_999 * 12 + 19_000 * 12:,} cas".replace(",", " ") in readme
