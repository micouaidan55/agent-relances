import pytest

import evals.run as run
from evals.generate import build_scenarios


def test_rules_and_facts_are_perfect_with_the_fallback_writer():
    scenarios, reminders, cost, stopped = run.evaluate(build_scenarios(), None, "claude-sonnet-5",
                                                       "Atelier Lumière SAS", on_line=lambda _: None)
    assert [s.id for s in scenarios if not s.rules_ok] == []
    assert reminders and all(r.facts_ok for r in reminders)
    assert cost == 0 and not stopped


def test_budget_stops_the_run(monkeypatch):
    from agent_relances import prepare as prepare_module
    from agent_relances.models import Usage

    original = prepare_module.prepare_draft

    def costly(api, due, company, model):
        prepared = original(None, due, company, model)
        prepared.usage = Usage(cost_usd=0.5)
        return prepared

    monkeypatch.setattr(prepare_module, "prepare_draft", costly)
    scenarios, _, cost, stopped = run.evaluate(build_scenarios(), None, "claude-sonnet-5", "A", max_cost=1.0,
                                               on_line=lambda _: None)
    assert stopped and cost >= 1.0 and len(scenarios) < 30


def test_run_exits_without_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(run, "load_dotenv", lambda: None)
    monkeypatch.setattr("sys.argv", ["run"])
    with pytest.raises(SystemExit):
        run.main()
