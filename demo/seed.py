"""Base de démonstration : factures émises d'Atelier Lumière SAS, paiements, fiches clients et relances déjà faites.

Usage : uv run python -m demo.seed [--reset]
"""

import argparse
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from pathlib import Path

from agent_factures.extraction.models import Direction, DocumentType, Invoice
from agent_factures.storage.db import connect as connect_factures
from agent_factures.storage.repository import InvoiceRepository

from agent_relances.drafting.fallback import fallback_draft
from agent_relances.models import Client, DraftSource, DueReminder, OverdueInvoice, Profile, Stage, Tone
from agent_relances.outbox import reference
from agent_relances.storage.db import connect as connect_relances
from agent_relances.storage.recovery import RecoveryStore
from agent_relances.storage.repository import ReminderStore

DEMO_TODAY = date(2026, 10, 15)
COMPANY = "Atelier Lumière SAS"
ROOT = Path(__file__).resolve().parent.parent

CLIENTS = [
    Client(name="Hôtel des Canuts", email="compta@hotel-canuts.fr", contact="Paul Girard",
           notes="Client fidèle depuis 2019, paie toujours dans les temps."),
    Client(name="Bistrot Saint-Paul", email="julien@bistrot-saint-paul.fr", contact="Julien Roche"),
    Client(name="Hôtel Bellevue", email="compta@hotel-bellevue.fr", contact="Anne Morel"),
    Client(name="Galerie Nova", email="m.petit@galerie-nova.fr", contact="Marc Petit"),
    Client(name="Sophie Leroy", email="sophie.leroy@gmail.com", contact="Sophie Leroy", professional=False),
    Client(name="Café des Arts", email="anne@cafedesarts.fr", contact="Anne Roux"),
    Client(name="Boulangerie Faure", email="p.faure@boulangerie-faure.fr", contact="Pierre Faure"),
    Client(name="Restaurant Le Gourmet", email="h.blanc@le-gourmet.fr", contact="Henri Blanc"),
    Client(name="Traiteur Marchand", email="compta@traiteur-marchand.fr", contact="Luc Marchand"),
    Client(name="Imprimerie Dupont", email="contact@imprimerie-dupont.fr", contact="Claire Dupont"),
]


def _write_reply(inbox: Path, name: str, sender: tuple[str, str], subject: str, body: str, today: date) -> None:
    message = EmailMessage()
    message["From"] = formataddr(sender)
    message["To"] = "relances@atelier-lumiere.fr"
    message["Subject"] = subject
    message["Message-ID"] = f"<demo-{name}@demo.local>"
    message["Date"] = format_datetime(datetime.combine(today, time(9, 30)).astimezone())
    message.set_content(body)
    (inbox / f"{name}.eml").write_bytes(message.as_bytes())


def seed(factures_path: Path, relances_path: Path, today: date = DEMO_TODAY, inbox_path: Path | None = None) -> None:
    """Recrée les deux bases et la boîte de réception de démonstration (`demo_inbox` à côté de la base des relances)."""
    inbox = Path(inbox_path) if inbox_path else Path(relances_path).parent / "demo_inbox"
    for path in (Path(factures_path), Path(relances_path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    for old in inbox.glob("*.eml"):
        old.unlink()
    repo = InvoiceRepository(connect_factures(str(factures_path)))
    relances_conn = connect_relances(str(relances_path))
    store, recovery = ReminderStore(relances_conn), RecoveryStore(relances_conn)
    clients = {client.name: client for client in CLIENTS}
    for client in CLIENTS:
        store.save_client(client)

    def invoice(number: str, customer: str, amount: str, due_days_ago: int, paid_after_due: int | None = None):
        due = today - timedelta(days=due_days_ago)
        ttc = Decimal(amount)
        ht = (ttc / Decimal("1.2")).quantize(Decimal("0.01"))
        data = Invoice(doc_type=DocumentType.INVOICE, direction=Direction.ISSUED, supplier=COMPANY, customer=customer,
                       number=number, issue_date=due - timedelta(days=30), due_date=due,
                       amount_excl_tax=ht, vat_amount=ttc - ht, amount_incl_tax=ttc)
        invoice_id = repo.add(data, f"demo/{number}.pdf")
        if paid_after_due is not None:
            repo.mark_paid(invoice_id, due + timedelta(days=paid_after_due))
        return invoice_id, data

    def past_reminder(customer: str, items, stage: Stage, days_ago: int) -> int:
        decided = today - timedelta(days=days_ago)
        invoices = [OverdueInvoice(invoice_id=i, number=d.number, customer=customer, amount=d.amount_incl_tax,
                                   issue_date=d.issue_date, due_date=d.due_date, days_late=(decided - d.due_date).days)
                    for i, d in items]
        due = DueReminder(client=clients[customer], profile=Profile.OCCASIONAL, stage=stage, tone=Tone.NEUTRAL,
                          invoices=invoices, invoice_stages={i: int(stage) for i, _ in items},
                          legal_notice=stage is Stage.FINAL and clients[customer].professional, today=decided)
        return store.add_validated(due, fallback_draft(due, COMPANY), decided, source=DraftSource.FALLBACK)

    # Historique de paiement : bon payeur, occasionnel, retardataire chronique.
    for n, (days_ago, late) in enumerate([(150, 2), (110, 1), (70, 3)]):
        invoice(f"AL-2026-0{40 + n}", "Hôtel des Canuts", "1840.00", days_ago, late)
    for n, (days_ago, late) in enumerate([(140, 10), (90, 14)]):
        invoice(f"AL-2026-0{50 + n}", "Bistrot Saint-Paul", "720.00", days_ago, late)
    for n, (days_ago, late) in enumerate([(160, 30), (100, 40)]):
        invoice(f"AL-2026-0{60 + n}", "Galerie Nova", "3200.00", days_ago, late)

    # 1. Bon payeur, 5 jours de retard : rappel courtois, ton léger.
    invoice("AL-2026-118", "Hôtel des Canuts", "2450.00", 5)
    # 2. Occasionnel, palier 1 fait il y a 12 jours : relance ferme, ton neutre.
    bistrot = invoice("AL-2026-104", "Bistrot Saint-Paul", "1180.40", 20)
    bistrot_ref = past_reminder("Bistrot Saint-Paul", [bistrot], Stage.COURTEOUS, 12)
    # 3. Deux factures en retard en même temps : un seul mail, profil retardataire, ton neutre au palier 1.
    invoice("AL-2026-113", "Hôtel Bellevue", "3960.00", 9)
    invoice("AL-2026-119", "Hôtel Bellevue", "1275.60", 4)
    # 4. Professionnel, paliers 1 et 2 faits : dernier rappel avec la mention des 40 €.
    nova = invoice("AL-2026-097", "Galerie Nova", "5820.00", 34)
    past_reminder("Galerie Nova", [nova], Stage.COURTEOUS, 30)
    nova_ref = past_reminder("Galerie Nova", [nova], Stage.FIRM, 18)
    # 5. Particulier au palier 3 : pas de mention des 40 €.
    leroy = invoice("AL-2026-099", "Sophie Leroy", "389.90", 33)
    past_reminder("Sophie Leroy", [leroy], Stage.COURTEOUS, 29)
    leroy_ref = past_reminder("Sophie Leroy", [leroy], Stage.FIRM, 16)
    # 6. Échue depuis 2 jours : pas encore de relance.
    invoice("AL-2026-121", "Café des Arts", "210.00", 2)
    # 7. Trois paliers faits : passe à un humain.
    faure = invoice("AL-2026-088", "Boulangerie Faure", "1540.00", 55)
    for stage, days_ago in ((Stage.COURTEOUS, 51), (Stage.FIRM, 39), (Stage.FINAL, 24)):
        faure_ref = past_reminder("Boulangerie Faure", [faure], stage, days_ago)
    # 8. Client sans fiche : « fiche à compléter ».
    invoice("AL-2026-116", "Studio Cohen", "2100.00", 10)
    # 9. Payée après une relance : compte dans « encaissé après relance ».
    gourmet = invoice("AL-2026-090", "Restaurant Le Gourmet", "860.00", 40, paid_after_due=10)
    gourmet_ref = past_reminder("Restaurant Le Gourmet", [gourmet], Stage.COURTEOUS, 36)

    # 10. Échéancier en cours, mensualité 1 en retard depuis 40 jours, relances J+3, J+15, J+30 validées :
    #     le contentieux est proposable (la 3e relance date de plus de 7 jours).
    marchand = invoice("AL-2026-075", "Traiteur Marchand", "1800.00", 100)
    agreed = today - timedelta(days=70)
    active_id = recovery.create_plan("Traiteur Marchand", [marchand[0]], Decimal("1800.00"), [Decimal("450.00")] * 4,
                                     agreed - timedelta(days=2))
    recovery.activate_plan(active_id, agreed)
    first_due = recovery.get_plan(active_id).installments[0].due_date
    for stage, late in ((1, 3), (2, 15), (3, 30)):
        recovery.record_installment_reminder(active_id, 1, stage, first_due + timedelta(days=late), None)

    # 11. Échéancier proposé, en attente de l'accord du client (la réponse « accord » de la démo).
    dupont = invoice("AL-2026-110", "Imprimerie Dupont", "960.00", 12)
    proposed_id = recovery.create_plan("Imprimerie Dupont", [dupont[0]], Decimal("960.00"), [Decimal("320.00")] * 3,
                                       today - timedelta(days=3))

    # Boîte de réception de démonstration : 6 réponses rattachées par la référence de l'objet.
    replies = [
        ("promesse_date", ("Julien Roche", "julien@bistrot-saint-paul.fr"),
         f"Re: Relance facture AL-2026-104 [Réf. {reference('R', bistrot_ref)}]",
         "Bonjour,\n\nExcusez ce retard, je vous règle le 30 octobre sans faute.\n\nJulien Roche"),
        ("demande_echeancier", ("Marc Petit", "m.petit@galerie-nova.fr"),
         f"RE: Dernier rappel facture AL-2026-097 [Réf. {reference('R', nova_ref)}]",
         "Bonjour,\n\nNotre trésorerie est tendue ce trimestre. Pourrions-nous régler cette facture en 6 fois ?\n\n"
         "Cordialement,\nMarc Petit"),
        ("contestation", ("Sophie Leroy", "sophie.leroy@gmail.com"),
         f"Re: Relance facture AL-2026-099 [Réf. {reference('R', leroy_ref)}]",
         "Bonjour,\n\nMerci pour votre message. Je règlerai volontiers dès que le montant sera corrigé : la facture "
         "comprend une prestation qui n'a jamais été réalisée.\n\nSophie Leroy"),
        ("paiement_annonce", ("Henri Blanc", "h.blanc@le-gourmet.fr"),
         f"Re: Relance facture AL-2026-090 [Réf. {reference('R', gourmet_ref)}]",
         "Bonjour,\n\nJ'ai payé cette facture par virement hier, merci de vérifier votre compte.\n\nHenri Blanc"),
        ("reponse_frauduleuse", ("Pierre Faure", "compta@boulangerie-faure.com"),
         f"Re: Dernier rappel facture AL-2026-088 [Réf. {reference('R', faure_ref)}]",
         "Bonjour,\n\nSuite à un changement de banque, merci de noter notre nouveau RIB pour tout remboursement : "
         "IBAN FR76 3000 6000 0112 3456 7890 189.\n\nPierre Faure"),
        ("accord_echeancier", ("Claire Dupont", "contact@imprimerie-dupont.fr"),
         f"Re: Proposition d'échéancier [Réf. {reference('E', proposed_id)}]",
         "Bonjour,\n\nNous sommes d'accord pour l'échéancier en trois mensualités que vous nous proposez.\n\n"
         "Cordialement,\nClaire Dupont"),
    ]
    for n, (name, sender, subject, body) in enumerate(replies, start=1):
        _write_reply(inbox, f"reponse_{n}_{name}", sender, subject, body, today)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factures", type=Path, default=ROOT / "data" / "demo_factures.db")
    parser.add_argument("--relances", type=Path, default=ROOT / "data" / "relances.db")
    parser.add_argument("--inbox", type=Path, default=None,
                        help="Dossier des réponses de démonstration (par défaut : demo_inbox à côté de la base des relances).")
    parser.add_argument("--reset", action="store_true", help="Remplace les bases existantes.")
    args = parser.parse_args()
    existing = [p for p in (args.factures, args.relances) if p.exists()]
    if existing and not args.reset:
        print("Bases déjà présentes : " + ", ".join(map(str, existing)) + ". Relance avec --reset pour les remplacer.")
        sys.exit(1)
    seed(args.factures, args.relances, inbox_path=args.inbox)
    print(f"Démonstration prête (date du jour : {DEMO_TODAY:%d/%m/%Y}). Mets TODAY={DEMO_TODAY.isoformat()} dans .env.")
    print("Six réponses de clients sont déposées dans demo_inbox/ (à côté de la base des relances) : "
          "AGENT_RELANCES_INBOX=data/demo_inbox.")


if __name__ == "__main__":
    main()
