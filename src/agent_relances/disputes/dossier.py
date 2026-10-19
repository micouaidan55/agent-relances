"""Dossier de contentieux pour l'avocat : PDF, copie des factures, brouillon .eml. Rien n'est jamais envoyé."""

import shutil
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from agent_relances.formatting import date_fr, euros
from agent_relances.models import Client, OverdueInvoice, Plan, PlanStatus, ReminderStatus
from agent_relances.outbox import _write_eml
from agent_relances.sources import InvoiceSource
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore, StoredReminder

PDF_NAME = "dossier_recouvrement.pdf"
EML_NAME = "brouillon_avocat.eml"
_TO_COMPLETE = "à compléter"


@dataclass
class DossierResult:
    folder: Path
    pdf_path: Path
    eml_path: Path
    copied: list[str]
    missing: list[str]
    total_due: Decimal


def _new_folder(out_root: Path, client: Client, today: date) -> Path:
    """Dossier `<client sans espaces>_<AAAA-MM-JJ>` ; un dossier existant n'est jamais réutilisé (_2, _3…)."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    base = f"{client.key.replace(' ', '')}_{today.isoformat()}"
    number = 1
    while True:
        folder = out_root / (base if number == 1 else f"{base}_{number}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            number += 1


def _copy_files(invoices: list[OverdueInvoice], files: dict[int, str], folder: Path, base_dir: Path):
    copied: list[str] = []
    missing: list[str] = []
    done: dict[Path, str] = {}  # un même fichier source n'est copié qu'une fois
    for invoice in invoices:
        raw = files.get(invoice.invoice_id)
        if not raw:
            missing.append(f"(fichier inconnu pour la facture {invoice.number})")
            continue
        source = Path(raw) if Path(raw).is_absolute() else Path(base_dir) / raw
        if not source.is_file():
            missing.append(Path(raw).name)
            continue
        if source.resolve() in done:
            continue
        name = source.name
        if (folder / name).exists() or name in (PDF_NAME, EML_NAME):
            name = f"{invoice.number}_{source.name}"
        counter = 2
        while (folder / name).exists():
            name = f"{invoice.number}_{counter}_{source.name}"
            counter += 1
        shutil.copy2(source, folder / name)
        done[source.resolve()] = name
        copied.append(name)
    return copied, missing


def _p(text: str, style) -> Paragraph:
    return Paragraph(escape(str(text)), style)  # texte de client ou de modèle : jamais interprété comme balisage


def _table(rows, widths, styles) -> Table:
    cells = [[_p(cell, styles["BodyText"]) if not isinstance(cell, Paragraph) else cell for cell in row]
             for row in rows]
    table = Table(cells, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return table


def _write_pdf(path, client, invoices, total, remaining, timeline, replies, plan, copied, missing,
               company, today) -> None:
    styles = getSampleStyleSheet()
    h, body = styles["Heading2"], styles["BodyText"]
    story = [_p("Dossier de recouvrement", styles["Title"]),
             _p(f"Établi le {date_fr(today)} pour {company}", body), Spacer(1, 0.3 * cm),
             _p("Client", h),
             _p(f"{client.name}" + (f" - contact : {client.contact}" if client.contact else "")
                + (f" - {client.email}" if client.email else ""), body),
             _p("Factures impayées", h)]
    rows = [["Facture", "Date", "Montant TTC", "Échéance"]]
    rows += [[i.number, date_fr(i.issue_date), euros(i.amount), date_fr(i.due_date)] for i in invoices]
    story.append(_table(rows, [5 * cm, 3 * cm, 4 * cm, 3 * cm], styles))
    story += [Spacer(1, 0.2 * cm), _p(f"Total des factures : {euros(total)}", body),
              _p(f"Reste dû : {euros(remaining)}", styles["Heading3"])]

    story.append(_p("Chronologie des relances", h))
    if timeline:
        story.append(_table([["Date", "Relance"]] + [[date_fr(d), label] for d, label in timeline],
                            [3 * cm, 12 * cm], styles))
    else:
        story.append(_p("Aucune relance validée.", body))

    story.append(_p("Réponses du client", h))
    if replies:
        story.append(_table([["Reçue le", "Résumé"]] + [[str(r.get("received_at", ""))[:10], r.get("summary", "")]
                                                         for r in replies], [3 * cm, 12 * cm], styles))
    else:
        story.append(_p("Aucune réponse du client.", body))

    if plan is not None:
        story.append(_p(f"Échéancier ({plan.status.value})", h))
        rows = [["Mensualité", "Montant", "Échéance", "Situation"]]
        for item in plan.installments:
            if item.received_on is not None:
                state = f"reçue le {date_fr(item.received_on)}"
            elif item.due_date is not None and item.due_date < today:
                state = "manquée"
            else:
                state = "à venir"
            rows.append([str(item.number), euros(item.amount), date_fr(item.due_date) if item.due_date else "-", state])
        story.append(_table(rows, [3 * cm, 4 * cm, 3 * cm, 5 * cm], styles))

    story.append(_p("Pièces jointes au dossier", h))
    story += [_p(f"Fichier copié : {name}", body) for name in copied]
    story += [_p(f"Fichier absent (introuvable) : {name}", body) for name in missing]
    if not copied and not missing:
        story.append(_p("Aucune pièce.", body))
    SimpleDocTemplate(str(path), pagesize=A4, title="Dossier de recouvrement", author=company).build(story)


def _eml_body(client: Client, lawyer_email: str, remaining: Decimal, count: int, folder: Path, company: str) -> str:
    recipient = lawyer_email or _TO_COMPLETE
    return (
        f"Bonjour Maître,\n\n(destinataire : {recipient})\n\n"
        f"Je vous transmets le dossier de recouvrement de notre client {client.name} : {count} facture(s) impayée(s), "
        f"pour un reste dû de {euros(remaining)}.\n\n"
        f"Le dossier complet (récapitulatif PDF, copies des factures, historique des relances) se trouve dans le "
        f"dossier : {folder}\n\n"
        f"Merci de me dire quelles suites vous conseillez.\n\nCordialement,\n{company}\n"
    )


def build_dossier(
    client: Client, invoices: list[OverdueInvoice], files: dict[int, str], reminders: list[StoredReminder],
    replies: list, plan: Plan | None, out_root: Path, lawyer_email: str, company: str, today: date, base_dir: Path,
    installment_reminders: dict[int, list[tuple[int, date]]] | None = None,
) -> DossierResult:
    """Construit le dossier et le brouillon pour l'avocat ; n'envoie rien."""
    total = sum((i.amount for i in invoices), Decimal("0"))
    received = sum((i.amount for i in plan.installments if i.received_on is not None), Decimal("0")) if plan else 0
    remaining = total - received
    timeline = sorted(
        [(r.decided_on, f"{r.subject}") for r in reminders
         if r.status is ReminderStatus.VALIDATED and r.decided_on is not None]
        + [(decided, f"Relance de la mensualité {number} (palier {stage})")
           for number, items in (installment_reminders or {}).items() for stage, decided in items],
        key=lambda item: item[0],
    )
    folder = _new_folder(out_root, client, today)
    copied, missing = _copy_files(invoices, files, folder, base_dir)
    pdf_path = folder / PDF_NAME
    _write_pdf(pdf_path, client, invoices, total, remaining, timeline, replies, plan, copied, missing, company, today)
    lawyer = Client(name="Avocat", email=lawyer_email.strip())
    eml_path = Path(_write_eml(
        folder, EML_NAME, f"contentieux-{folder.name}", lawyer,
        f"Dossier de recouvrement - {client.name}",
        _eml_body(client, lawyer.email, remaining, len(invoices), folder, company),
    ))
    return DossierResult(folder=folder, pdf_path=pdf_path, eml_path=eml_path, copied=copied, missing=missing,
                         total_due=remaining)


def open_dispute(
    client: Client, recovery: RecoveryStore, reminders: ReminderStore, source: InvoiceSource, out_root: Path,
    lawyer_email: str, company: str, today: date, base_dir: Path,
) -> DossierResult:
    """Ouvre le contentieux : dossier, échéancier en cours rompu, suspension sans fin, enregistrement, journal."""
    key = client.key
    if any(d["client_key"] == key for d in recovery.list_disputes()):
        raise ValueError(f"Un contentieux est déjà ouvert pour {client.name}.")
    invoices = source.unpaid_of(key, today)
    if not invoices:
        raise ValueError(f"{client.name} n'a aucune facture impayée : pas de contentieux possible.")
    plan = recovery.open_plan_for(key)
    done = recovery.installment_reminders(plan.id) if plan else None
    result = build_dossier(
        client, invoices, source.files_of([i.invoice_id for i in invoices]),
        [r for r in reminders.list_reminders({ReminderStatus.VALIDATED}) if r.client_key == key],
        [r for r in recovery.list_replies() if r["client_key"] == key], plan, out_root, lawyer_email, company, today,
        base_dir, installment_reminders=done,
    )
    if plan is not None:
        if plan.status is PlanStatus.ACTIVE:
            recovery.set_plan_status(plan.id, PlanStatus.BROKEN, today)
        else:  # proposé : jamais accepté, il ne doit plus retenir les factures hors du cycle de relances
            recovery.set_plan_status(plan.id, PlanStatus.CANCELLED, today)
    recovery.add_hold(key, "suspension", None, "contentieux", today)
    recovery.add_dispute(key, str(result.folder), result.total_due, today)
    reminders.log(None, "contentieux_ouvert", {"client": key, "dossier": str(result.folder),
                                               "reste_du": str(result.total_due), "manquants": result.missing})
    return result
