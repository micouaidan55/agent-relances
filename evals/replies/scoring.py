"""Métriques de l'évaluation de la compréhension des réponses de clients.

Chaque taux a son propre dénominateur (affiché « n/m ») : une date n'est jugée que là où une date est attendue, et
l'absence de date inventée là où aucune n'est attendue.
"""

from dataclasses import dataclass
from statistics import mean

from evals.scoring import pct, rate


@dataclass
class ReplyOutcome:
    id: str
    title: str
    intent_ok: bool
    matching_ok: bool                 # règle du code (match_reply), pas le modèle
    action_ok: bool                   # propose_action (code) appliquée à la lecture du modèle
    fraud_expected: bool
    flagged: bool                     # règle du code (reply_is_suspicious), pas le modèle
    cost_usd: float
    got: dict
    expected: dict
    date_ok: bool | None = None       # None : aucune date attendue
    date_not_invented: bool | None = None   # None : une date est attendue
    months_ok: bool | None = None     # None : aucun nombre attendu
    months_not_invented: bool | None = None
    error: str = ""


def _of(outcomes, field):
    return [getattr(o, field) for o in outcomes if getattr(o, field) is not None]


def summarize(outcomes: list[ReplyOutcome]) -> dict:
    frauds = [o for o in outcomes if o.fraud_expected]
    honest = [o for o in outcomes if not o.fraud_expected]
    fields = {"intent": "intent_ok", "date": "date_ok", "no_date": "date_not_invented", "months": "months_ok",
              "no_months": "months_not_invented", "matching": "matching_ok", "action": "action_ok"}
    summary = {"replies": len(outcomes)}
    for name, field in fields.items():
        values = _of(outcomes, field)
        summary[f"{name}_accuracy"] = rate(values)
        summary[f"{name}_count"] = [sum(values), len(values)]
    summary.update({
        "fraud_blocked": rate([o.flagged for o in frauds]),
        "fraud_count": [sum(o.flagged for o in frauds), len(frauds)],
        "false_positives": sum(o.flagged for o in honest),
        "honest_count": len(honest),
        "mean_cost_usd": mean(o.cost_usd for o in outcomes) if outcomes else 0.0,
        "total_cost_usd": sum(o.cost_usd for o in outcomes),
    })
    return summary


def _cell(summary: dict, name: str) -> str:
    good, total = summary[f"{name}_count"]
    return f"{pct(summary[f'{name}_accuracy'])} ({good}/{total})"


def to_markdown(label: str, summary: dict, outcomes: list[ReplyOutcome]) -> str:
    lines = [
        f"## Résultats — compréhension des réponses, `{label}` ({summary['replies']} réponses)", "",
        "| Métrique | Valeur |", "|---|---|",
        f"| Intention correcte (modèle) | {_cell(summary, 'intent')} |",
        f"| Date promise correcte, là où une date est attendue (modèle + règle du code) | {_cell(summary, 'date')} |",
        f"| Aucune date inventée, là où aucune n'est attendue (modèle + règle du code) | {_cell(summary, 'no_date')} |",
        f"| Nombre de mensualités correct, là où il est écrit (modèle) | {_cell(summary, 'months')} |",
        f"| Aucun nombre inventé, là où aucun n'est écrit (modèle) | {_cell(summary, 'no_months')} |",
        f"| Rattachement correct (règle du code, sans modèle) | {_cell(summary, 'matching')} |",
        f"| Action proposée correcte, toutes réponses (code appliqué à la lecture du modèle) | {_cell(summary, 'action')} |",
        f"| Fraudes bloquées (règle du code, sans modèle) | {pct(summary['fraud_blocked'])}"
        f" ({summary['fraud_count'][0]}/{summary['fraud_count'][1]}) |",
        f"| Réponses légitimes bloquées à tort (règle du code) | {summary['false_positives']}/{summary['honest_count']} |",
        f"| Coût moyen par réponse | {summary['mean_cost_usd']:.4f} $ |",
    ]
    wrong = [o for o in outcomes if False in (o.intent_ok, o.matching_ok, o.action_ok, o.date_ok, o.date_not_invented,
                                              o.months_ok, o.months_not_invented) or o.flagged != o.fraud_expected]
    if wrong:
        lines += ["", "### Écarts", ""]
        for o in wrong:
            lines.append(f"- {o.id} ({o.title}) : attendu {o.expected}, obtenu {o.got}"
                         + (f" — {o.error}" if o.error else ""))
    return "\n".join(lines) + "\n"
