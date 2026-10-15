import json
from itertools import product

from evals.generate import build_scenarios, generate

PROFILES = ["bon_payeur", "occasionnel", "retardataire_chronique"]


def reminders():
    return [(s, r) for s in build_scenarios() for r in s["expected"]["reminders"]]


def test_composition():
    scenarios = build_scenarios()
    assert len(scenarios) == 30 and len({s["id"] for s in scenarios}) == 30
    cells = {(r["stage"], r["profile"]) for _, r in reminders()}
    assert set(product([1, 2, 3], PROFILES)) <= cells
    assert any(s["expected"]["missing"] for s in scenarios)
    assert any(s["expected"]["escalated"] for s in scenarios)
    assert any(len(r["numbers"]) == 3 for _, r in reminders())
    assert any(not s["expected"]["reminders"] and not s["expected"]["missing"] and not s["expected"]["escalated"]
               for s in scenarios)


def test_legal_notice_matches_the_spec():
    clients = {(s["id"], c["name"]): c for s in build_scenarios() for c in s["clients"]}
    for scenario, reminder in reminders():
        professional = clients[(scenario["id"], reminder["client"])]["professional"]
        assert reminder["legal"] == (reminder["stage"] == 3 and professional)
        assert reminder["tone"] in {"leger", "neutre", "ferme"}
    assert any(r["stage"] == 3 and not r["legal"] for _, r in reminders())


def test_generate_writes_the_dataset(tmp_path):
    path = tmp_path / "scenarios.json"
    generate(path)
    assert json.loads(path.read_text()) == build_scenarios()
