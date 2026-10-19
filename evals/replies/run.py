"""Évalue la compréhension des réponses de clients sur ~45 cas. ATTENTION : appelle l'API Claude (payant).

Seuls l'intention, la date et le nombre de mensualités viennent du modèle. Le rattachement (`match_reply`), la
détection de fraude (`reply_is_suspicious`) et l'action proposée (`propose_action`) sont des règles du code : elles
sont rapportées avec la lecture du modèle, sans coût. `ReplyReading` n'a pas de drapeau de fraude.

Usage : uv run python -m evals.replies.run --max-cost 1
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import anthropic
from agent_emails.models import Email, Link
from dotenv import load_dotenv

from agent_relances.drafting.llm import READER_MODEL, StepError
from agent_relances.models import Client, DueReminder, OverdueInvoice, Profile, ReminderDraft, Stage, Tone, Usage
from agent_relances.models import ActionKind, ReplyReading
from agent_relances.replies.actions import propose_action, reply_is_suspicious
from agent_relances.replies.matching import match_reply
from agent_relances.replies.reader import promised_date_of, read_reply
from agent_relances.storage.db import connect
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore
from evals.replies.generate import CLIENTS, COMPANY_DOMAIN, PLAN_CLIENTS, TODAY
from evals.replies.scoring import ReplyOutcome, summarize, to_markdown

ROOT = Path(__file__).parent
COMPANY = "Atelier Lumière SAS"


def build_stores() -> tuple[ReminderStore, RecoveryStore]:
    """Fiches clients de l'évaluation, une relance (R-0000i) par client ; un échéancier proposé seulement pour les
    clients de PLAN_CLIENTS (E-00001…), les autres n'en ont aucun."""
    conn = connect()
    reminders, recovery = ReminderStore(conn), RecoveryStore(conn)
    today = date.fromisoformat(TODAY)
    for number, card in enumerate(CLIENTS, start=1):
        client = Client(**card)
        reminders.save_client(client)
        invoice = OverdueInvoice(invoice_id=number, number=f"AL-{number}", customer=client.name,
                                 amount=Decimal("600.00"), issue_date=today, due_date=today, days_late=10)
        due = DueReminder(client=client, profile=Profile.OCCASIONAL, stage=Stage.COURTEOUS, tone=Tone.NEUTRAL,
                          invoices=[invoice], invoice_stages={number: 1}, legal_notice=False, today=today)
        reminders.add_validated(due, ReminderDraft(subject="(relance)", body="(relance)"), today)
        if number - 1 in PLAN_CLIENTS:
            recovery.create_plan(client.name, [number], Decimal("600.00"), [Decimal("300.00")] * 2, today)
    return reminders, recovery


def to_email(item: dict) -> Email:
    return Email(message_id=f"<eval-{item['id']}@exemple.fr>", sender_name=item["sender_name"],
                 sender_address=item["sender_address"], subject=item["subject"],
                 received_at=datetime.fromisoformat(f"{TODAY}T09:00:00"), body=item["body"],
                 reply_to=item["reply_to"], links=[Link(**link) for link in item["links"]])


def evaluate_replies(replies: list[dict], api, model: str, company: str, today: date, max_cost: float | None = None,
                     on_line=print, outcomes: list[ReplyOutcome] | None = None, totals: dict | None = None) -> dict:
    """La liste `outcomes` et `totals` fournis par l'appelant sont remplis au fil de l'eau : un plantage ne perd rien."""
    outcomes = [] if outcomes is None else outcomes
    totals = {} if totals is None else totals
    totals.setdefault("cost", 0.0)
    totals["stopped"] = False
    reminders, recovery = build_stores()
    cards = {c.name: c for c in reminders.list_clients()}
    for item in replies:
        expected, email = item["expected"], to_email(item)
        usage, error, reading, promised = Usage(), "", None, None
        try:
            reading, usage = read_reply(api, email, company, today, model=model)
            promised = promised_date_of(reading, today)
        except StepError as exc:
            usage, error = exc.usage, str(exc)
        except anthropic.APIError as exc:
            error = type(exc).__name__
        totals["cost"] += usage.cost_usd
        match = match_reply(email, reminders, recovery)
        found = reminders.get_client(match.client_key) if match.client_key else None
        flagged = reply_is_suspicious(email, [found.email] if found else [], COMPANY_DOMAIN)
        if found is None:  # réponse non rattachée : enregistrée sans action (voir process_replies)
            action = ActionKind.NONE
        elif reading is not None:
            action = propose_action(reading, flagged, True, recovery.open_plan_for(found.key), today).kind
        else:  # lecture impossible : process_replies retombe sur « autre »
            action = propose_action(ReplyReading(intent="autre", summary=""), flagged, True,
                                    recovery.open_plan_for(found.key), today).kind
        got = {"intent": reading.intent.value if reading else None,
               "promised_date": promised.isoformat() if promised else None,
               "installments": reading.requested_installments if reading else None,
               "client": found.name if found else None, "action": action.value}
        ok = reading is not None
        outcomes.append(ReplyOutcome(
            id=item["id"], title=item["title"], intent_ok=got["intent"] in expected["accepted_intents"],
            matching_ok=got["client"] == expected["client"], action_ok=action.value in expected["accepted_actions"],
            fraud_expected=expected["fraud"], flagged=flagged, cost_usd=usage.cost_usd, got=got,
            expected={k: expected[k] for k in ("intent", "promised_date", "installments", "client", "action")},
            date_ok=(ok and got["promised_date"] == expected["promised_date"]) if expected["promised_date"] else None,
            date_not_invented=(ok and got["promised_date"] is None) if not expected["promised_date"] else None,
            months_ok=(ok and got["installments"] == expected["installments"]) if expected["installments"] else None,
            months_not_invented=(ok and got["installments"] is None) if not expected["installments"] else None,
            error=error))
        on_line(f"{item['id']} {'OK ' if outcomes[-1].intent_ok else 'ÉCART'} {item['title']}"
                f" — cumul {totals['cost']:.4f} $")
        if max_cost is not None and totals["cost"] >= max_cost:
            on_line(f"Budget de {max_cost:.2f} $ atteint : arrêt après {len(outcomes)} réponses.")
            totals["stopped"] = True
            break
    return {**summarize(outcomes), "stopped": totals["stopped"], "outcomes": outcomes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=READER_MODEL)
    parser.add_argument("--max-cost", type=float, default=None, help="Budget en $ au-delà duquel l'évaluation s'arrête.")
    parser.add_argument("--dataset", type=Path, default=ROOT / "dataset" / "replies.json")
    parser.add_argument("--out", type=Path, default=ROOT.parent / "results")
    args = parser.parse_args()
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Clé d'API Claude manquante : renseigne ANTHROPIC_API_KEY dans le fichier .env avant de lancer l'évaluation.")
        sys.exit(1)
    replies = json.loads(args.dataset.read_text())
    api = anthropic.Anthropic()
    outcomes: list[ReplyOutcome] = []
    totals: dict = {"cost": 0.0}
    try:
        evaluate_replies(replies, api, args.model, COMPANY, date.fromisoformat(TODAY), max_cost=args.max_cost,
                         outcomes=outcomes, totals=totals)
    finally:
        partial = len(outcomes) < len(replies)
        label = args.model + (f" (partiel : {len(outcomes)}/{len(replies)} réponses)" if partial else "")
        stem = "reponses__" + args.model + ("__partiel" if partial else "")
        summary = summarize(outcomes)
        report = to_markdown(label, summary, outcomes)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{stem}.md").write_text(report)
        (args.out / f"{stem}.json").write_text(json.dumps(
            {"model": args.model, "today": TODAY, "summary": summary, "outcomes": [o.__dict__ for o in outcomes]},
            ensure_ascii=False, indent=2))
        print("\n" + report)
        print(f"Coût total de l'évaluation : {totals['cost']:.4f} $")


if __name__ == "__main__":
    main()
