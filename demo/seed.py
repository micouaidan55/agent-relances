"""Base de démonstration : factures émises d'Atelier Lumière SAS, paiements, fiches clients et relances déjà faites.

Usage : uv run python -m demo.seed [--reset]
"""

import argparse
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from agent_factures.extraction.models import Direction, DocumentType, Invoice
from agent_factures.storage.db import connect as connect_factures
from agent_factures.storage.repository import InvoiceRepository

from agent_relances.drafting.fallback import fallback_draft
from agent_relances.models import Client, DueReminder, OverdueInvoice, Profile, Stage, Tone
from agent_relances.storage.db import connect as connect_relances
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
]


def seed(factures_path: Path, relances_path: Path, today: date = DEMO_TODAY) -> None:
    for path in (Path(factures_path), Path(relances_path)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
    repo = InvoiceRepository(connect_factures(str(factures_path)))
    store = ReminderStore(connect_relances(str(relances_path)))
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

    def past_reminder(customer: str, items, stage: Stage, days_ago: int) -> None:
        decided = today - timedelta(days=days_ago)
        invoices = [OverdueInvoice(invoice_id=i, number=d.number, customer=customer, amount=d.amount_incl_tax,
                                   issue_date=d.issue_date, due_date=d.due_date, days_late=(decided - d.due_date).days)
                    for i, d in items]
        due = DueReminder(client=clients[customer], profile=Profile.OCCASIONAL, stage=stage, tone=Tone.NEUTRAL,
                          invoices=invoices, invoice_stages={i: int(stage) for i, _ in items},
                          legal_notice=stage is Stage.FINAL and clients[customer].professional, today=decided)
        store.add_validated(due, fallback_draft(due, COMPANY), decided)

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
    past_reminder("Bistrot Saint-Paul", [bistrot], Stage.COURTEOUS, 12)
    # 3. Deux factures en retard en même temps : un seul mail, profil retardataire, ton neutre au palier 1.
    invoice("AL-2026-113", "Hôtel Bellevue", "3960.00", 9)
    invoice("AL-2026-119", "Hôtel Bellevue", "1275.60", 4)
    # 4. Professionnel, paliers 1 et 2 faits : dernier rappel avec la mention des 40 €.
    nova = invoice("AL-2026-097", "Galerie Nova", "5820.00", 34)
    past_reminder("Galerie Nova", [nova], Stage.COURTEOUS, 30)
    past_reminder("Galerie Nova", [nova], Stage.FIRM, 18)
    # 5. Particulier au palier 3 : pas de mention des 40 €.
    leroy = invoice("AL-2026-099", "Sophie Leroy", "389.90", 33)
    past_reminder("Sophie Leroy", [leroy], Stage.COURTEOUS, 29)
    past_reminder("Sophie Leroy", [leroy], Stage.FIRM, 16)
    # 6. Échue depuis 2 jours : pas encore de relance.
    invoice("AL-2026-121", "Café des Arts", "210.00", 2)
    # 7. Trois paliers faits : passe à un humain.
    faure = invoice("AL-2026-088", "Boulangerie Faure", "1540.00", 55)
    for stage, days_ago in ((Stage.COURTEOUS, 51), (Stage.FIRM, 39), (Stage.FINAL, 24)):
        past_reminder("Boulangerie Faure", [faure], stage, days_ago)
    # 8. Client sans fiche : « fiche à compléter ».
    invoice("AL-2026-116", "Studio Cohen", "2100.00", 10)
    # 9. Payée après une relance : compte dans « encaissé après relance ».
    gourmet = invoice("AL-2026-090", "Restaurant Le Gourmet", "860.00", 40, paid_after_due=10)
    past_reminder("Restaurant Le Gourmet", [gourmet], Stage.COURTEOUS, 36)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factures", type=Path, default=ROOT / "data" / "demo_factures.db")
    parser.add_argument("--relances", type=Path, default=ROOT / "data" / "relances.db")
    parser.add_argument("--reset", action="store_true", help="Remplace les bases existantes.")
    args = parser.parse_args()
    existing = [p for p in (args.factures, args.relances) if p.exists()]
    if existing and not args.reset:
        print("Bases déjà présentes : " + ", ".join(map(str, existing)) + ". Relance avec --reset pour les remplacer.")
        sys.exit(1)
    seed(args.factures, args.relances)
    print(f"Démonstration prête (date du jour : {DEMO_TODAY:%d/%m/%Y}). Mets TODAY={DEMO_TODAY.isoformat()} dans .env.")


if __name__ == "__main__":
    main()
