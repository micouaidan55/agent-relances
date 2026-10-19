"""Modèle de texte de secours : déterministe, utilisé quand le modèle échoue ou que la clé d'API manque."""

from agent_relances.formatting import date_fr, euros
from agent_relances.models import Client, DueReminder, Plan, ReminderDraft, Stage, Tone
from agent_relances.rules.installments import FIRST_DUE_DAYS
from agent_relances.rules.legal import LEGAL_NOTICE

_SUBJECTS = {
    Stage.COURTEOUS: "Rappel : {what} en attente de règlement",
    Stage.FIRM: "Relance : {what} impayée{s}",
    Stage.FINAL: "Dernier rappel avant mise en demeure : {what}",
}
_INTROS = {
    Stage.COURTEOUS: "Sauf erreur de notre part, le règlement de {the} suivante{s} ne nous est pas encore parvenu :",
    Stage.FIRM: "Malgré notre précédent rappel, {the} suivante{s} reste{nt} impayée{s} :",
    Stage.FINAL: "Malgré nos relances, {the} suivante{s} reste{nt} impayée{s} :",
}
_ASKS = {
    Stage.COURTEOUS: "Pourriez-vous nous indiquer la date de règlement prévue ? Si le paiement a été effectué "
                     "entre-temps, merci de ne pas tenir compte de ce message.",
    Stage.FIRM: "Nous vous remercions de procéder au règlement dans les meilleurs délais et de nous confirmer la "
                "date de paiement.",
    Stage.FINAL: "Sans règlement de votre part, nous serons contraints d'engager une procédure de recouvrement.",
}
_CLOSINGS = {
    Tone.LIGHT: "Il s'agit sans doute d'un simple oubli. Merci d'avance pour votre retour.",
    Tone.NEUTRAL: "Nous vous remercions par avance de votre retour.",
    Tone.FIRM: "Nous comptons sur un règlement rapide.",
}


def fallback_draft(due: DueReminder, company: str) -> ReminderDraft:
    several = len(due.invoices) > 1
    numbers = ", ".join(invoice.number for invoice in due.invoices)
    words = {
        "what": f"factures {numbers}" if several else f"facture {numbers}",
        "the": "les factures" if several else "la facture",
        "s": "s" if several else "",
        "nt": "nt" if several else "",
    }
    lines = [f"- facture n° {i.number} : {euros(i.amount)} TTC, échue le {date_fr(i.due_date)}" for i in due.invoices]
    if several:
        lines.append(f"Total dû : {euros(due.total)} TTC")
    paragraphs = [
        f"Bonjour {due.client.contact}," if due.client.contact else "Bonjour,",
        _INTROS[due.stage].format(**words) + "\n" + "\n".join(lines),
        _ASKS[due.stage],
    ]
    if due.legal_notice:
        paragraphs.append(LEGAL_NOTICE)
    paragraphs += [_CLOSINGS[due.tone], f"Cordialement,\n{company}"]
    return ReminderDraft(subject=_SUBJECTS[due.stage].format(**words), body="\n\n".join(paragraphs))


def _plan_lines(plan: Plan) -> list[str]:
    ordered = sorted(plan.installments, key=lambda i: i.number)
    return [f"- mensualité {i.number} : {euros(i.amount)}" + (f", le {date_fr(i.due_date)}" if i.due_date else "")
            for i in ordered]


def fallback_proposal(plan: Plan, client: Client, company: str, requested: int | None) -> ReminderDraft:
    count = len(plan.installments)
    asked = (f"Vous nous demandiez {requested} mois ; selon nos conditions, nous pouvons vous proposer "
             f"{count} mensualités. " if requested and requested != count else "")
    paragraphs = [
        f"Bonjour {client.contact}," if client.contact else "Bonjour,",
        f"{asked}Pour régler les {euros(plan.total)} TTC restant dus, nous vous proposons un échéancier en "
        f"{count} mensualités :\n" + "\n".join(_plan_lines(plan)),
        f"La première mensualité serait due {FIRST_DUE_DAYS} jours après votre accord, puis chaque mois à la même "
        "date. Pour accepter, il suffit de répondre à ce message.",
        f"Cordialement,\n{company}",
    ]
    return ReminderDraft(subject="Proposition d'échéancier de paiement", body="\n\n".join(paragraphs))


def fallback_confirmation(plan: Plan, client: Client, company: str) -> ReminderDraft:
    paragraphs = [
        f"Bonjour {client.contact}," if client.contact else "Bonjour,",
        f"Nous vous confirmons l'échéancier convenu pour le règlement de {euros(plan.total)} TTC :\n"
        + "\n".join(_plan_lines(plan)),
        "Nous vous remercions par avance.",
        f"Cordialement,\n{company}",
    ]
    return ReminderDraft(subject="Confirmation de votre échéancier de paiement", body="\n\n".join(paragraphs))
