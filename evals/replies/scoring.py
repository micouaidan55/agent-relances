"""Métriques de l'évaluation de la compréhension des réponses de clients."""

from dataclasses import dataclass
from statistics import mean

from evals.scoring import _pct, _rate


@dataclass
class ReplyOutcome:
    id: str
    title: str
    intent_ok: bool
    date_ok: bool
    months_ok: bool
    matching_ok: bool
    fraud_expected: bool
    flagged: bool          # `reply_is_suspicious` (code seul : ReplyReading n'a pas de drapeau de fraude)
    cost_usd: float
    got: dict
    expected: dict
    error: str = ""


def summarize(outcomes: list[ReplyOutcome]) -> dict:
    frauds = [o for o in outcomes if o.fraud_expected]
    honest = [o for o in outcomes if not o.fraud_expected]
    return {
        "replies": len(outcomes),
        "intent_accuracy": _rate([o.intent_ok for o in outcomes]),
        "date_accuracy": _rate([o.date_ok for o in outcomes]),
        "months_accuracy": _rate([o.months_ok for o in outcomes]),
        "matching_accuracy": _rate([o.matching_ok for o in outcomes]),
        "fraud_blocked": _rate([o.flagged for o in frauds]),
        "frauds": len(frauds),
        "false_positives": sum(o.flagged for o in honest),
        "mean_cost_usd": mean(o.cost_usd for o in outcomes) if outcomes else 0.0,
        "total_cost_usd": sum(o.cost_usd for o in outcomes),
    }


def to_markdown(label: str, summary: dict, outcomes: list[ReplyOutcome]) -> str:
    lines = [
        f"## Résultats — compréhension des réponses, `{label}` ({summary['replies']} réponses)", "",
        "| Métrique | Valeur |", "|---|---|",
        f"| Intention correcte | {_pct(summary['intent_accuracy'])} |",
        f"| Date promise correcte (après la règle du code) | {_pct(summary['date_accuracy'])} |",
        f"| Nombre de mensualités correct | {_pct(summary['months_accuracy'])} |",
        f"| Rattachement correct | {_pct(summary['matching_accuracy'])} |",
        f"| Fraudes bloquées (règle du code, {summary['frauds']} cas) | {_pct(summary['fraud_blocked'])} |",
        f"| Réponses légitimes bloquées à tort | {summary['false_positives']} |",
        f"| Coût moyen par réponse | {summary['mean_cost_usd']:.4f} $ |",
    ]
    wrong = [o for o in outcomes if not (o.intent_ok and o.date_ok and o.months_ok and o.matching_ok)
             or o.flagged != o.fraud_expected]
    if wrong:
        lines += ["", "### Écarts", ""]
        for o in wrong:
            lines.append(f"- {o.id} ({o.title}) : attendu {o.expected}, obtenu {o.got}" + (f" — {o.error}" if o.error else ""))
    return "\n".join(lines) + "\n"
