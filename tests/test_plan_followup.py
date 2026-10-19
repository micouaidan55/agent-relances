from datetime import date, timedelta
from decimal import Decimal

import pytest

from agent_relances.drafting.checker import check_plan_draft
from agent_relances.models import Installment, Plan, PlanStatus, Stage
from agent_relances.plans import cancel_plan, installment_reminder_draft, refresh_plans
from agent_relances.rules.installments import dispute_due, installment_steps
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from tests.factories import client

D = Decimal
DUE = date(2026, 11, 15)


def plan(received=(), status=PlanStatus.ACTIVE, plan_id=7):
    items = [Installment(number=n, amount=D("250.00"), due_date=DUE + timedelta(days=30 * (n - 1)),
                         received_on=DUE if n in received else None) for n in (1, 2, 3)]
    return Plan(id=plan_id, client_key="hotel des canuts", invoice_ids=[1, 2], total=D("750.00"), status=status,
                proposed_on=date(2026, 10, 1), agreed_on=date(2026, 10, 16), installments=items)


def steps(done=None, today=DUE, last_contact=None, **kw):
    return installment_steps(kw.pop("plan", plan()), done or {}, last_contact, today, **kw)


def test_two_days_late_nothing():
    assert steps(today=DUE + timedelta(days=2)) == []


def test_three_days_late_first_stage():
    [due] = steps(today=DUE + timedelta(days=3))
    assert (due.plan_id, due.number, due.amount, due.due_date, due.stage, due.days_late) == (
        7, 1, D("250.00"), DUE, Stage.COURTEOUS, 3)


def test_not_yet_due_or_received_or_inactive_plan():
    assert steps(today=DUE - timedelta(days=1)) == []
    assert steps(plan=plan(received=(1,)), today=DUE + timedelta(days=3)) == []
    assert steps(plan=plan(status=PlanStatus.BROKEN), today=DUE + timedelta(days=3)) == []


def test_second_stage_needs_first_validated_and_day_15():
    first = {1: [(1, DUE + timedelta(days=3))]}
    assert steps(today=DUE + timedelta(days=14), done=first) == []
    [due] = steps(today=DUE + timedelta(days=15), done=first)
    assert due.stage is Stage.FIRM
    assert steps(today=DUE + timedelta(days=20), done={})[0].stage is Stage.COURTEOUS  # pas de saut de palier


def test_third_stage_then_nothing():
    done = {1: [(1, DUE + timedelta(days=3)), (2, DUE + timedelta(days=15))]}
    [due] = steps(today=DUE + timedelta(days=30), done=done)
    assert due.stage is Stage.FINAL
    done[1].append((3, DUE + timedelta(days=30)))
    assert [d.number for d in steps(today=DUE + timedelta(days=60), done=done)] == [2]  # la mensualité 1 est épuisée


def test_minimum_gap_since_last_contact():
    today = DUE + timedelta(days=3)
    assert steps(today=today, last_contact=today - timedelta(days=6)) == []
    assert len(steps(today=today, last_contact=today - timedelta(days=7))) == 1


def test_dispute_after_seven_days_not_before():
    done = {1: [(1, DUE), (2, DUE), (3, DUE + timedelta(days=30))]}
    assert not dispute_due(plan(), done, DUE + timedelta(days=36))
    assert dispute_due(plan(), done, DUE + timedelta(days=37))
    assert not dispute_due(plan(received=(1,)), done, DUE + timedelta(days=60))
    assert not dispute_due(plan(status=PlanStatus.BROKEN), done, DUE + timedelta(days=60))
    assert not dispute_due(plan(), {1: [(1, DUE), (2, DUE)]}, DUE + timedelta(days=60))


class Source:
    def __init__(self, unpaid):
        self._unpaid = unpaid

    def unpaid_ids(self):
        return set(self._unpaid)

    def numbers_of(self, invoice_ids):
        return {i: f"AL-{i}" for i in invoice_ids}


@pytest.fixture
def recovery():
    return RecoveryStore(connect())


def active_plan(recovery, invoice_ids=(1, 2)):
    plan_id = recovery.create_plan("hotel des canuts", list(invoice_ids), D("500.00"), [D("250.00")] * 2,
                                   date(2026, 10, 1))
    recovery.activate_plan(plan_id, date(2026, 10, 16))
    return plan_id


def test_refresh_settles_when_all_covered_invoices_paid(recovery):
    plan_id = active_plan(recovery)
    assert refresh_plans(recovery, Source({1, 2}), date(2026, 11, 1)) == []
    assert refresh_plans(recovery, Source({2}), date(2026, 11, 1)) == []
    messages = refresh_plans(recovery, Source({9}), date(2026, 11, 2))
    assert len(messages) == 1 and "E-00001" in messages[0]
    assert recovery.get_plan(plan_id).status is PlanStatus.SETTLED
    assert refresh_plans(recovery, Source(set()), date(2026, 11, 3)) == []


def test_refresh_settles_when_all_installments_received(recovery):
    plan_id = active_plan(recovery)
    recovery.mark_received(plan_id, 1, date(2026, 11, 15))
    assert refresh_plans(recovery, Source({1, 2}), date(2026, 11, 16)) == []
    recovery.mark_received(plan_id, 2, date(2026, 12, 15))
    [message] = refresh_plans(recovery, Source({1, 2}), date(2026, 12, 16))
    assert recovery.get_plan(plan_id).status is PlanStatus.SETTLED
    assert "AL-1, AL-2" in message and "payées dans agent-factures" in message


def test_refresh_ignores_proposed_plans(recovery):
    plan_id = recovery.create_plan("hotel des canuts", [1], D("500.00"), [D("250.00")] * 2, date(2026, 10, 1))
    assert refresh_plans(recovery, Source(set()), date(2026, 11, 1)) == []
    assert recovery.get_plan(plan_id).status is PlanStatus.PROPOSED


@pytest.mark.parametrize("stage", [Stage.COURTEOUS, Stage.FIRM, Stage.FINAL])
def test_installment_draft_passes_plan_check(stage):
    [due] = steps(today=DUE + timedelta(days=30), done={1: [(s, DUE) for s in range(1, stage)]})[:1]
    assert due.stage is stage
    draft = installment_reminder_draft(due, client(), "Atelier Lumière SAS")
    assert draft.subject.endswith("[Réf. E-00007]")
    assert "Paul Girard" in draft.body and "Atelier Lumière SAS" in draft.body
    assert check_plan_draft(draft, [due.amount], None, [due.due_date], require_count=False) == []


def test_distinct_tone_per_stage():
    bodies = set()
    for stage in Stage:
        [due] = steps(today=DUE + timedelta(days=30), done={1: [(s, DUE) for s in range(1, stage)]})
        bodies.add(installment_reminder_draft(due, client(), "X").body)
    assert len(bodies) == 3


def test_cancel_plan_refuses_an_active_plan_with_a_received_installment(recovery):
    plan_id = active_plan(recovery)
    recovery.mark_received(plan_id, 1, date(2026, 11, 15))
    [error] = cancel_plan(recovery, plan_id, date(2026, 11, 20))
    assert "contentieux" in error
    assert recovery.get_plan(plan_id).status is PlanStatus.ACTIVE


def test_cancel_plan_cancels_a_proposed_or_untouched_plan(recovery):
    plan_id = active_plan(recovery)
    assert cancel_plan(recovery, plan_id, date(2026, 11, 20)) == []
    assert recovery.get_plan(plan_id).status is PlanStatus.CANCELLED
    assert cancel_plan(recovery, plan_id, date(2026, 11, 20))                  # déjà clos


def test_merged_last_contacts_take_the_latest_of_both_kinds():
    from agent_relances.plans import merged_last_contacts

    class Reminders:
        def last_contacts(self):
            return {"a": date(2026, 10, 1), "b": date(2026, 10, 9)}

    class Recovery:
        def last_installment_contacts(self):
            return {"a": date(2026, 10, 5), "c": date(2026, 10, 2)}

    assert merged_last_contacts(Reminders(), Recovery()) == {
        "a": date(2026, 10, 5), "b": date(2026, 10, 9), "c": date(2026, 10, 2)}
