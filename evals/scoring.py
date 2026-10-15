"""Métriques de l'évaluation des relances."""

from dataclasses import dataclass, field
from statistics import mean


@dataclass
class ScenarioOutcome:
    id: str
    rules_ok: bool
    differences: list[str] = field(default_factory=list)


@dataclass
class ReminderOutcome:
    scenario: str
    client: str
    expected_tone: str
    source: str
    first_try_ok: bool
    facts_ok: bool
    judge: dict | None
    cost_usd: float
    seconds: float


def _rate(values: list[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(scenarios: list[ScenarioOutcome], reminders: list[ReminderOutcome]) -> dict:
    judged = [r for r in reminders if r.judge]
    return {
        "scenarios": len(scenarios),
        "rules_accuracy": _rate([s.rules_ok for s in scenarios]),
        "reminders": len(reminders),
        "facts_accuracy": _rate([r.facts_ok for r in reminders]),
        "first_try_rate": _rate([r.first_try_ok for r in reminders]),
        "fallback_rate": _rate([r.source == "secours" for r in reminders]),
        "tone_accuracy": _rate([r.judge["ton"] == r.expected_tone for r in judged]),
        "judge_mean": mean(mean(r.judge[k] for k in ("clarte", "politesse", "appel_action")) for r in judged)
        if judged else None,
        "mean_cost_usd": mean(r.cost_usd for r in reminders) if reminders else 0.0,
        "mean_seconds": mean(r.seconds for r in reminders) if reminders else 0.0,
    }


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f} %".replace(".", ",") if value is not None else "non évalué"


def to_markdown(label: str, summary: dict, scenarios: list[ScenarioOutcome]) -> str:
    judge = f"{summary['judge_mean']:.2f} / 5".replace(".", ",") if summary["judge_mean"] is not None else "non évalué"
    lines = [
        f"## Résultats — `{label}` ({summary['scenarios']} scénarios, {summary['reminders']} relances)", "",
        "| Métrique | Valeur |", "|---|---|",
        f"| Règles (relance, palier, regroupement, mention) | {_pct(summary['rules_accuracy'])} |",
        f"| Faits exacts dans les relances proposées | {_pct(summary['facts_accuracy'])} |",
        f"| Faits exacts dès le premier essai | {_pct(summary['first_try_rate'])} |",
        f"| Recours au modèle de secours | {_pct(summary['fallback_rate'])} |",
        f"| Ton reconnu à l'aveugle | {_pct(summary['tone_accuracy'])} |",
        f"| Note moyenne du juge | {judge} |",
        f"| Coût moyen par relance | {summary['mean_cost_usd']:.4f} $ |",
        f"| Temps moyen par relance | {summary['mean_seconds']:.1f} s |",
    ]
    failures = [s for s in scenarios if not s.rules_ok]
    if failures:
        lines += ["", "### Écarts sur les règles", ""] + [f"- {s.id} : {'; '.join(s.differences)}" for s in failures]
    return "\n".join(lines) + "\n"
