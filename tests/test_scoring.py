from evals.scoring import ReminderOutcome, ScenarioOutcome, summarize, to_markdown


def reminder(source="modele", first=True, facts=True, judge=None, cost=0.006, tone="leger"):
    return ReminderOutcome(scenario="S01", client="A", expected_tone=tone, source=source, first_try_ok=first,
                           facts_ok=facts, judge=judge, cost_usd=cost, seconds=3.0)


def test_summarize():
    scenarios = [ScenarioOutcome("S01", True, []), ScenarioOutcome("S02", False, ["palier attendu 2, obtenu 1"])]
    reminders = [
        reminder(judge={"ton": "leger", "clarte": 5, "politesse": 5, "appel_action": 4}),
        reminder(source="secours", first=False, judge={"ton": "neutre", "clarte": 4, "politesse": 4, "appel_action": 4}),
    ]
    summary = summarize(scenarios, reminders)
    assert summary["rules_accuracy"] == 0.5 and summary["facts_accuracy"] == 1.0
    assert summary["first_try_rate"] == 0.5 and summary["fallback_rate"] == 0.5
    assert summary["tone_accuracy"] == 0.5
    assert abs(summary["judge_mean"] - (14 / 3 + 4) / 2) < 1e-9
    assert abs(summary["mean_cost_usd"] - 0.006) < 1e-12


def test_summarize_without_judge_and_markdown():
    summary = summarize([ScenarioOutcome("S01", True, [])], [reminder()])
    assert summary["tone_accuracy"] is None and summary["judge_mean"] is None
    report = to_markdown("claude-sonnet-5", summary, [ScenarioOutcome("S02", False, ["écart"])])
    for text in ("Règles", "Faits exacts", "Ton reconnu à l'aveugle", "non évalué", "S02 : écart"):
        assert text in report
