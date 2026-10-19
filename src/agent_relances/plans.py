"""Suivi des échéanciers en cours : clôture des plans réglés et relances de mensualités."""

from datetime import date

from agent_relances.formatting import date_fr, euros
from agent_relances.models import Client, PlanStatus, ReminderDraft, Stage
from agent_relances.outbox import reference, with_reference
from agent_relances.rules.installments import InstallmentDue
from agent_relances.sources import InvoiceSource
from agent_relances.storage.recovery import RecoveryStore


def refresh_plans(recovery: RecoveryStore, source: InvoiceSource, today: date) -> list[str]:
    """Passe à « soldé » les plans en cours dont toutes les mensualités sont reçues ou dont toutes les factures
    couvertes sont payées dans agent-factures. Renvoie un message par plan clôturé."""
    unpaid = source.unpaid_ids()
    messages = []
    for plan in recovery.list_plans([PlanStatus.ACTIVE]):
        all_received = all(i.received_on is not None for i in plan.installments)
        all_paid = bool(plan.invoice_ids) and not unpaid.intersection(plan.invoice_ids)
        if (all_received or all_paid) and recovery.set_plan_status(plan.id, PlanStatus.SETTLED, today):
            messages.append(f"Échéancier {reference('E', plan.id)} soldé : plus rien à recouvrer.")
    return messages


_SUBJECTS = {
    Stage.COURTEOUS: "Rappel : mensualité de votre échéancier en attente",
    Stage.FIRM: "Relance : mensualité de votre échéancier impayée",
    Stage.FINAL: "Dernier rappel avant mise en demeure : mensualité de votre échéancier",
}
_INTROS = {
    Stage.COURTEOUS: "Sauf erreur de notre part, la mensualité n° {n} de votre échéancier ne nous est pas encore "
                     "parvenue :",
    Stage.FIRM: "Malgré notre précédent rappel, la mensualité n° {n} de votre échéancier reste impayée :",
    Stage.FINAL: "Malgré nos relances, la mensualité n° {n} de votre échéancier reste impayée :",
}
_ASKS = {
    Stage.COURTEOUS: "Pourriez-vous nous indiquer la date de règlement prévue ? Si le paiement a été effectué "
                     "entre-temps, merci de ne pas tenir compte de ce message.",
    Stage.FIRM: "Nous vous remercions de procéder au règlement dans les meilleurs délais et de nous confirmer la "
                "date de paiement.",
    Stage.FINAL: "Sans règlement de votre part, nous serons contraints d'engager une procédure de recouvrement.",
}
_CLOSINGS = {
    Stage.COURTEOUS: "Il s'agit sans doute d'un simple oubli. Merci d'avance pour votre retour.",
    Stage.FIRM: "Nous vous remercions par avance de votre retour.",
    Stage.FINAL: "Nous comptons sur un règlement rapide.",
}


def installment_reminder_draft(due: InstallmentDue, client: Client, company: str) -> ReminderDraft:
    """Texte déterministe : courtois, puis ferme, puis dernier rappel. Ne cite que ce montant et cette échéance."""
    stage = Stage(due.stage)
    paragraphs = [
        f"Bonjour {client.contact}," if client.contact else "Bonjour,",
        _INTROS[stage].format(n=due.number) + f"\n- {euros(due.amount)} TTC, échue le {date_fr(due.due_date)}",
        _ASKS[stage],
        _CLOSINGS[stage],
        f"Cordialement,\n{company}",
    ]
    return ReminderDraft(subject=with_reference(_SUBJECTS[stage], reference("E", due.plan_id)),
                         body="\n\n".join(paragraphs))
