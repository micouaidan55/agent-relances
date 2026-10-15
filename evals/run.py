"""Lance la préparation des relances sur les 30 scénarios. ATTENTION : appelle l'API Claude (payant).

Usage : uv run python -m evals.run --max-cost 1
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import anthropic
from agent_factures.extraction.models import Direction, DocumentType, Invoice
from agent_factures.storage.db import connect as connect_factures
from agent_factures.storage.repository import InvoiceRepository
from dotenv import load_dotenv

import agent_relances.prepare as prepare
from agent_relances.drafting.checker import check_draft
from agent_relances.drafting.llm import JUDGE_MODEL, StepError, WRITER_MODEL
from agent_relances.models import (
    Client, DueReminder, OverdueInvoice, Profile, ReminderDraft, Stage, Tone,
)
from agent_relances.sources import InvoiceSource
from agent_relances.storage.db import connect as connect_relances
from agent_relances.storage.repository import ReminderStore
from evals.judge import judge_reminder
from evals.scoring import ReminderOutcome, ScenarioOutcome, summarize, to_markdown

ROOT = Path(__file__).parent
COMPANY = "Atelier Lumière SAS"


def _invoice(number: str, customer: str, amount: str, due: date) -> Invoice:
    ttc = Decimal(amount)
    ht = (ttc / Decimal("1.2")).quantize(Decimal("0.01"))
    return Invoice(doc_type=DocumentType.INVOICE, direction=Direction.ISSUED, supplier=COMPANY, customer=customer,
                   number=number, issue_date=due - timedelta(days=30), due_date=due,
                   amount_excl_tax=ht, vat_amount=ttc - ht, amount_incl_tax=ttc)


def build(scenario: dict) -> tuple[InvoiceSource, ReminderStore, date]:
    today = date.fromisoformat(scenario["today"])
    conn = connect_factures()
    repo = InvoiceRepository(conn)
    ids: dict[str, tuple[int, Invoice]] = {}
    for item in scenario["invoices"]:
        data = _invoice(item["number"], item["customer"], item["amount"], today - timedelta(days=item["due_days_ago"]))
        ids[item["number"]] = (repo.add(data, "eval"), data)
    for item in scenario["payments"]:
        due = today - timedelta(days=item["due_days_ago"])
        invoice_id = repo.add(_invoice(item["number"], item["customer"], item["amount"], due), "eval")
        repo.mark_paid(invoice_id, due + timedelta(days=item["paid_days_late"]))
    store = ReminderStore(connect_relances())
    clients = {c["name"]: Client(**c) for c in scenario["clients"]}
    for client in clients.values():
        store.save_client(client)
    for item in scenario["history"]:
        decided = today - timedelta(days=item["days_ago"])
        invoices = [OverdueInvoice(invoice_id=ids[n][0], number=n, customer=item["customer"],
                                   amount=ids[n][1].amount_incl_tax, issue_date=ids[n][1].issue_date,
                                   due_date=ids[n][1].due_date, days_late=(decided - ids[n][1].due_date).days)
                    for n in item["numbers"]]
        client = clients.get(item["customer"]) or Client(name=item["customer"], email="historique@exemple.fr")
        due = DueReminder(client=client, profile=Profile.OCCASIONAL, stage=Stage(item["stage"]), tone=Tone.NEUTRAL,
                          invoices=invoices, invoice_stages={i.invoice_id: item["stage"] for i in invoices},
                          legal_notice=False, today=decided)
        store.add_validated(due, ReminderDraft(subject="(historique)", body="(historique)"), decided)
    return InvoiceSource(conn), store, today


def compare(scenario: dict, result: prepare.PreparationResult) -> list[str]:
    expected = scenario["expected"]
    actual = sorted(({"client": p.due.client.name, "stage": int(p.due.stage), "tone": p.due.tone.value,
                      "profile": p.due.profile.value, "numbers": [i.number for i in p.due.invoices],
                      "legal": p.due.legal_notice} for p in result.prepared), key=lambda r: r["client"])
    differences = []
    if actual != expected["reminders"]:
        differences.append(f"relances attendues {expected['reminders']}, obtenues {actual}")
    if sorted(result.missing_clients) != expected["missing"]:
        differences.append(f"fiches manquantes attendues {expected['missing']}, obtenues {result.missing_clients}")
    escalated = sorted(i.number for i in result.escalated)
    if escalated != expected["escalated"]:
        differences.append(f"passages à un humain attendus {expected['escalated']}, obtenus {escalated}")
    return differences


def evaluate(scenarios: list[dict], api, model: str, company: str, judge_api=None, max_cost: float | None = None,
             on_line=print) -> tuple[list[ScenarioOutcome], list[ReminderOutcome], float, bool]:
    scenario_outcomes, reminder_outcomes, total = [], [], 0.0
    for scenario in scenarios:
        source, store, today = build(scenario)
        result = prepare.prepare_today(source, store, api, company, today, model=model)
        differences = compare(scenario, result)
        scenario_outcomes.append(ScenarioOutcome(scenario["id"], not differences, differences))
        expected_tones = {r["client"]: r["tone"] for r in scenario["expected"]["reminders"]}
        for prepared in result.prepared:
            total += prepared.usage.cost_usd
            judge = None
            if judge_api is not None:
                try:
                    score, usage = judge_reminder(judge_api, prepared.draft)
                    judge = score.model_dump(mode="json")
                    total += usage.cost_usd
                except StepError as exc:
                    total += exc.usage.cost_usd
                except anthropic.APIError:
                    pass
            reminder_outcomes.append(ReminderOutcome(
                scenario=scenario["id"], client=prepared.due.client.name,
                expected_tone=expected_tones.get(prepared.due.client.name, prepared.due.tone.value),
                source=prepared.source.value, first_try_ok=prepared.first_try_ok,
                facts_ok=not check_draft(prepared.draft, prepared.due), judge=judge,
                cost_usd=prepared.usage.cost_usd, seconds=prepared.seconds,
            ))
        on_line(f"{scenario['id']} {'OK ' if not differences else 'ÉCART'} {len(result.prepared)} relance(s)"
                f" — cumul {total:.4f} $")
        if max_cost is not None and total >= max_cost:
            on_line(f"Budget de {max_cost:.2f} $ atteint : arrêt après {len(scenario_outcomes)} scénarios.")
            return scenario_outcomes, reminder_outcomes, total, True
    return scenario_outcomes, reminder_outcomes, total, False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=WRITER_MODEL)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--max-cost", type=float, default=None, help="Budget en $ au-delà duquel l'évaluation s'arrête.")
    parser.add_argument("--dataset", type=Path, default=ROOT / "dataset" / "scenarios.json")
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    args = parser.parse_args()
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Clé d'API Claude manquante : renseigne ANTHROPIC_API_KEY dans le fichier .env avant de lancer l'évaluation.")
        sys.exit(1)
    scenarios = json.loads(args.dataset.read_text())
    api = anthropic.Anthropic()
    outcomes, reminders, total, stopped = [], [], 0.0, True
    try:
        outcomes, reminders, total, stopped = evaluate(scenarios, api, args.model, COMPANY,
                                                       judge_api=None if args.no_judge else api,
                                                       max_cost=args.max_cost)
    finally:
        partial = stopped or len(outcomes) < len(scenarios)
        label = args.model + (f" (partiel : {len(outcomes)}/{len(scenarios)} scénarios)" if partial else "")
        stem = args.model + ("__partiel" if partial else "")
        report = to_markdown(label, summarize(outcomes, reminders), outcomes)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{stem}.md").write_text(report)
        (args.out / f"{stem}.json").write_text(json.dumps([r.__dict__ for r in reminders], ensure_ascii=False, indent=2))
        print("\n" + report)
        print(f"Coût total de l'évaluation (juge compris) : {total:.4f} $")


if __name__ == "__main__":
    main()
