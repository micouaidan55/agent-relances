import json
from collections import Counter
from datetime import date

import pytest

import evals.replies.run as run
from agent_emails.models import Email
from agent_relances.models import ReplyIntent, ReplyReading
from agent_relances.replies.actions import reply_is_suspicious
from agent_relances.replies.reader import promised_date_of
from evals.replies.generate import COMPANY_DOMAIN, TODAY, build_replies
from tests.fakes import FakeClient, reply

TODAY_DATE = date.fromisoformat(TODAY)


def perfect_readings(replies):
    return [reply(ReplyReading(intent=ReplyIntent(r["expected"]["intent"]),
                               promised_date=r["expected"]["promised_date"],
                               requested_installments=r["expected"]["installments"], summary="ok"))
            for r in replies]


def costly(readings, tokens=1_000_000):
    return [reply(r.parsed_output, input_tokens=tokens, output_tokens=0) for r in readings]


def evaluate(replies, readings, **kwargs):
    return run.evaluate_replies(replies, FakeClient(readings), "claude-haiku-4-5", "Atelier Lumière SAS", TODAY_DATE,
                                on_line=lambda _: None, **kwargs)


def test_dataset_composition():
    replies = build_replies()
    assert 38 <= len(replies) <= 48
    assert len({r["id"] for r in replies}) == len(replies)
    intents = Counter(r["expected"]["intent"] for r in replies)
    assert set(intents) == {i.value for i in ReplyIntent}
    assert all(count >= 5 for count in intents.values())
    assert sum(r["expected"]["fraud"] for r in replies) >= 3


def test_dataset_has_the_trap_cases():
    replies = build_replies()
    bodies = " ".join(r["body"].lower() for r in replies)
    assert any(r["expected"]["installments"] == 6 for r in replies)
    assert "trente novembre" in bodies and "fin du mois" in bodies
    assert any(r["expected"]["promised_date"] == "2026-11-30" for r in replies)
    assert any(r["expected"]["intent"] == "promesse_date" and r["expected"]["promised_date"] is None for r in replies)
    assert any("we agree" in r["body"].lower() or "i have paid" in r["body"].lower() for r in replies)
    assert any(r["expected"]["client"] is None for r in replies)
    cards = {c.email for c in run.build_stores()[0].list_clients()}
    assert any("[Réf. R-" in r["subject"] and r["sender_address"] not in cards for r in replies)


def test_dataset_file_is_the_generated_one():
    path = run.ROOT / "dataset" / "replies.json"
    assert json.loads(path.read_text()) == json.loads(json.dumps(build_replies()))


def test_expected_dates_are_concrete_and_not_in_the_past():
    for item in build_replies():
        expected = item["expected"]["promised_date"]
        if expected:
            assert date.fromisoformat(expected) >= TODAY_DATE


def test_the_rule_blocks_every_fraud_and_no_honest_reply():
    cards = run.build_stores()[0].list_clients()
    for item in build_replies():
        owner = [c.email for c in cards if c.name == item["expected"]["client"]]
        suspicious = reply_is_suspicious(run.to_email(item), owner, COMPANY_DOMAIN)
        assert suspicious is item["expected"]["fraud"], item["id"]


def test_perfect_fake_client_scores_100_percent():
    replies = build_replies()
    api = FakeClient(perfect_readings(replies))
    summary = run.evaluate_replies(replies, api, "claude-haiku-4-5", "A", TODAY_DATE, on_line=lambda _: None)
    for key in ("intent_accuracy", "date_accuracy", "no_date_accuracy", "months_accuracy", "no_months_accuracy",
                "matching_accuracy", "action_accuracy", "fraud_blocked"):
        assert summary[key] == 1.0, key
    assert summary["false_positives"] == 0 and not summary["stopped"]
    assert summary["replies"] == len(replies) and len(api.messages.calls) == len(replies)


def test_one_wrong_intent_lowers_intent_accuracy_only():
    replies = build_replies()
    readings = perfect_readings(replies)
    readings[0].parsed_output.intent = (ReplyIntent.DISPUTE if replies[0]["expected"]["intent"] != "contestation"
                                        else ReplyIntent.OTHER)
    summary = evaluate(replies, readings)
    assert summary["intent_accuracy"] == pytest.approx((len(replies) - 1) / len(replies))
    assert summary["date_accuracy"] == 1.0 and summary["matching_accuracy"] == 1.0


def test_wrong_date_and_months_are_counted():
    replies = build_replies()
    readings = perfect_readings(replies)
    readings[next(i for i, r in enumerate(replies) if r["expected"]["promised_date"])].parsed_output.promised_date = \
        "2026-12-24"
    readings[next(i for i, r in enumerate(replies) if r["expected"]["installments"])].parsed_output \
        .requested_installments = 9
    summary = evaluate(replies, readings)
    dates, months = summary["date_count"][1], summary["months_count"][1]
    assert dates == sum(1 for r in replies if r["expected"]["promised_date"]) and 0 < dates < len(replies)
    assert months == sum(1 for r in replies if r["expected"]["installments"]) and 0 < months < len(replies)
    assert summary["date_accuracy"] == pytest.approx((dates - 1) / dates)
    assert summary["months_accuracy"] == pytest.approx((months - 1) / months)
    assert summary["no_date_accuracy"] == 1.0 and summary["no_months_accuracy"] == 1.0
    assert summary["intent_accuracy"] == 1.0


def test_invented_date_and_count_are_counted_where_none_is_expected():
    replies = build_replies()
    readings = perfect_readings(replies)
    bare = next(i for i, r in enumerate(replies) if not r["expected"]["promised_date"] and
                not r["expected"]["installments"])
    readings[bare].parsed_output.promised_date = "2026-10-31"
    readings[bare].parsed_output.requested_installments = 2
    summary = evaluate(replies, readings)
    assert summary["no_date_count"][0] == summary["no_date_count"][1] - 1
    assert summary["no_months_count"][0] == summary["no_months_count"][1] - 1
    assert summary["date_accuracy"] == 1.0 and summary["months_accuracy"] == 1.0


def test_accord_without_proposal_has_no_plan_and_expects_a_human():
    item = next(r for r in build_replies() if r["title"] == "Accord sans proposition")
    assert item["expected"]["intent"] == "accord_echeancier" and item["expected"]["action"] == "humain"
    reminders, recovery = run.build_stores()
    assert recovery.open_plan_for(item["expected"]["client"]) is None
    other = next(r for r in build_replies() if r["title"] == "Accord simple")
    assert recovery.open_plan_for(other["expected"]["client"]) is not None
    assert other["expected"]["action"] == "activation_echeancier"


def test_wrong_intent_changes_the_proposed_action():
    replies = build_replies()
    index = next(i for i, r in enumerate(replies) if r["title"] == "Accord simple")
    readings = perfect_readings(replies)
    readings[index].parsed_output.intent = ReplyIntent.OTHER
    summary = evaluate(replies, readings)
    assert summary["action_count"] == [len(replies) - 1, len(replies)]


def test_vague_end_of_month_accepts_both_intents_and_actions():
    replies = build_replies()
    index = next(i for i, r in enumerate(replies) if r["title"].startswith("Fin du mois"))
    readings = perfect_readings(replies)
    readings[index].parsed_output.intent = ReplyIntent.PROMISED_DATE
    summary = evaluate(replies, readings)
    assert summary["intent_accuracy"] == 1.0 and summary["action_accuracy"] == 1.0


def test_api_error_counts_as_wrong_without_stopping():
    import anthropic
    import httpx

    replies = build_replies()[:3]
    good = perfect_readings(replies)
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    summary = evaluate(replies, [good[0], error, good[2]])
    assert summary["replies"] == 3 and summary["intent_accuracy"] == pytest.approx(2 / 3)


def test_partial_flag_is_off_when_every_reply_was_processed(monkeypatch, tmp_path):
    replies = build_replies()
    run_main(monkeypatch, tmp_path, FakeClient(costly(perfect_readings(replies), 1_000_000)), "--max-cost", "0.01",
             "--dataset", str(_one_reply_dataset(tmp_path, replies[0])))
    assert not list((tmp_path / "out").glob("*__partiel*"))
    assert list((tmp_path / "out").glob("reponses__*.md"))


def _one_reply_dataset(tmp_path, item):
    path = tmp_path / "un.json"
    path.write_text(json.dumps([item]))
    return path


def test_a_past_date_read_correctly_counts_as_no_date():
    replies = build_replies()
    index = next(i for i, r in enumerate(replies)
                 if r["expected"]["intent"] == "promesse_date" and r["expected"]["promised_date"] is None)
    readings = perfect_readings(replies)
    readings[index].parsed_output.promised_date = "2026-10-03"
    assert promised_date_of(readings[index].parsed_output, TODAY_DATE) is None
    assert evaluate(replies, readings)["date_accuracy"] == 1.0


def test_mean_cost_is_reported():
    replies = build_replies()[:4]
    summary = evaluate(replies, costly(perfect_readings(replies)))
    assert summary["mean_cost_usd"] == pytest.approx(1.0) and summary["total_cost_usd"] == pytest.approx(4.0)


def test_budget_stops_the_run_as_soon_as_the_cap_is_reached():
    replies = build_replies()
    api = FakeClient(costly(perfect_readings(replies)))
    summary = run.evaluate_replies(replies, api, "claude-haiku-4-5", "A", TODAY_DATE, max_cost=2.5,
                                   on_line=lambda _: None)
    assert summary["stopped"] and summary["replies"] == 3 and len(api.messages.calls) == 3
    assert summary["total_cost_usd"] == pytest.approx(3.0)


def test_unreadable_reply_counts_as_wrong_without_stopping():
    replies = build_replies()[:3]
    good = perfect_readings(replies)
    summary = evaluate(replies, [good[0], reply(None), reply(None), good[2]])
    assert summary["replies"] == 3 and summary["intent_accuracy"] == pytest.approx(2 / 3)


def run_main(monkeypatch, tmp_path, api, *extra):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fausse-cle")
    monkeypatch.setattr(run, "load_dotenv", lambda: None)
    monkeypatch.setattr(run.anthropic, "Anthropic", lambda: api)
    monkeypatch.setattr("sys.argv", ["run", "--out", str(tmp_path / "out"), *extra])
    run.main()


def test_results_written_as_json_and_markdown(monkeypatch, tmp_path):
    replies = build_replies()
    run_main(monkeypatch, tmp_path, FakeClient(perfect_readings(replies)), "--model", "claude-haiku-4-5")
    out = tmp_path / "out"
    data = json.loads((out / "reponses__claude-haiku-4-5.json").read_text())
    assert len(data["outcomes"]) == len(replies)
    text = (out / "reponses__claude-haiku-4-5.md").read_text()
    assert "100,0 %" in text and "Fraudes bloquées" in text


def test_crash_midway_keeps_partial_results(monkeypatch, tmp_path):
    readings = perfect_readings(build_replies())[:5] + [RuntimeError("plantage simulé")]
    with pytest.raises(RuntimeError):
        run_main(monkeypatch, tmp_path, FakeClient(readings))
    (report,) = (tmp_path / "out").glob("*__partiel.md")
    assert "partiel : 5/" in report.read_text()
    (data,) = (tmp_path / "out").glob("*__partiel.json")
    assert len(json.loads(data.read_text())["outcomes"]) == 5


def test_run_exits_without_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(run, "load_dotenv", lambda: None)
    monkeypatch.setattr("sys.argv", ["run"])
    with pytest.raises(SystemExit):
        run.main()


def test_to_email_builds_an_agent_emails_object():
    email = run.to_email(build_replies()[0])
    assert isinstance(email, Email) and email.subject and email.sender_address
