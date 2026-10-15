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


def _scenario_with_one_reminder():
    return [s for s in build_scenarios() if s["expected"]["reminders"]][:1]


def test_judge_is_blind_and_its_cost_is_counted():
    from evals.judge import JudgeScore
    from tests.fakes import FakeClient, reply

    judge = FakeClient([reply(JudgeScore(ton="leger", clarte=5, politesse=5, appel_action=5, commentaire="ok"),
                              input_tokens=1_000_000, output_tokens=0)])
    scenarios, reminders, cost, _ = run.evaluate(_scenario_with_one_reminder(), None, "claude-sonnet-5", "Atelier",
                                                 judge_api=judge, on_line=lambda _: None)
    assert len(judge.messages.calls) == 1 and abs(cost - 2.0) < 1e-9
    assert reminders[0].judge["ton"] == "leger"
    call = judge.messages.calls[0]
    content, system = call["messages"][0]["content"], call["system"]
    assert "Objet :" in content and "Atelier" in content
    for hidden in ("Palier", "palier", "Ton demandé", "Profil", "profil", "bon_payeur", "occasionnel",
                   "retardataire"):
        assert hidden not in content
    assert "Palier" not in system and "bon_payeur" not in system


def test_crash_midway_keeps_partial_results(monkeypatch, tmp_path):
    original = run.prepare.prepare_today
    calls = []

    def crashing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("plantage simulé")
        return original(*args, **kwargs)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fausse-cle")
    monkeypatch.setattr(run, "load_dotenv", lambda: None)
    monkeypatch.setattr(run.anthropic, "Anthropic", lambda: None)
    monkeypatch.setattr(run.prepare, "prepare_today", crashing)
    monkeypatch.setattr("sys.argv", ["run", "--no-judge", "--out", str(tmp_path)])
    with pytest.raises(RuntimeError):
        run.main()
    reports = list(tmp_path.glob("*__partiel.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "partiel : 2/30" in text and "2 scénarios" in text
