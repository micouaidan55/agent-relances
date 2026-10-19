"""Génère ~48 réponses de clients et leurs réponses attendues, écrites d'après la spec (jamais d'après le modèle).

Aujourd'hui vaut TODAY (comme l'évaluation des relances) : une date en toutes lettres a donc une valeur concrète.
Une date vague (« fin du mois »), absente ou passée est attendue à `null`.

Usage : uv run python -m evals.replies.generate
"""

import json
from pathlib import Path

TODAY = "2026-10-15"
COMPANY_DOMAIN = "atelier-lumiere.fr"
DATASET = Path(__file__).parent / "dataset" / "replies.json"

# Fiches clients de l'évaluation : la relance n° i+1 (R-0000i+1) a été envoyée au client d'indice i.
CLIENTS = [
    {"name": "Hôtel des Canuts", "email": "compta@hotel-canuts.fr", "contact": "Paul Girard"},
    {"name": "Bistrot Saint-Paul", "email": "julien@bistrot-saint-paul.fr", "contact": "Julien Roche"},
    {"name": "Galerie Nova", "email": "m.petit@galerie-nova.fr", "contact": "Marc Petit"},
    {"name": "Cabinet Benali", "email": "contact@cabinet-benali.fr", "contact": "Nadia Benali"},
    {"name": "Maison Garnier", "email": "e.garnier@maison-garnier.fr", "contact": "Élodie Garnier"},
    {"name": "Café des Arts", "email": "anne@cafedesarts.fr", "contact": "Anne Roux"},
    {"name": "Sophie Leroy", "email": "sophie.leroy@gmail.com", "contact": "Sophie Leroy"},
    {"name": "Librairie du Parc", "email": "compta@librairie-du-parc.fr", "contact": "Claire Martin"},
]
# Seuls ces clients (indices dans CLIENTS) ont un échéancier proposé, numérotés E-00001… dans cet ordre (voir run.py).
# Les autres, dont celui de « Accord sans proposition », n'en ont aucun.
PLAN_CLIENTS = [2, 3, 4, 5, 7]


def ref(kind: str, client: int) -> str:
    number = PLAN_CLIENTS.index(client) + 1 if kind == "E" else client + 1
    return f"[Réf. {kind}-{number:05d}]"


def expected_action(intent: str, date: str | None, fraud: bool, client: str | None, has_plan: bool) -> str:
    """Action que la spec attend (le client a des factures impayées) : un humain confirme toujours."""
    if client is None:
        return "sans_objet"
    if fraud:
        return "humain"
    if intent == "paiement_annonce":
        return "report"
    if intent == "promesse_date":
        return "report" if date else "humain"
    if intent == "demande_echeancier":
        return "proposition_echeancier"
    if intent == "accord_echeancier":
        return "activation_echeancier" if has_plan else "humain"
    if intent == "contestation":
        return "suspension"
    return "humain"


class Builder:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, title: str, client: int | None, body: str, intent: str, *, date: str | None = None,
            months: int | None = None, fraud: bool = False, via: str = "reference", sender: str | None = None,
            name: str = "", subject: str | None = None, kind: str = "R", reply_to: str = "", links=(),
            accepted_intents: list[str] | None = None, accepted_actions: list[str] | None = None):
        """`via` : reference (objet + adresse de la fiche), expediteur (adresse de la fiche, pas de référence),
        autre_adresse (référence mais adresse inconnue), reference_inconnue (référence fausse + adresse de la fiche),
        inconnu (ni l'un ni l'autre)."""
        card = CLIENTS[client] if client is not None else None
        if kind == "E" and client not in PLAN_CLIENTS:
            raise ValueError(f"{title} : ce client n'a pas d'échéancier proposé")
        subject = subject or ("Re: Relance de paiement" if kind == "R" else "Re: Votre échéancier")
        if via in ("reference", "autre_adresse") and card:
            subject += f" {ref(kind, client)}"
        elif via == "reference_inconnue":
            subject += " [Réf. R-99999]"
        address = sender or (card["email"] if card and via != "autre_adresse" else "inconnu@exemple.fr")
        expected_client = card["name"] if card and via != "inconnu" else None
        action = expected_action(intent, date, fraud, expected_client, kind == "E" and via != "inconnu")
        self.items.append({
            "id": f"R{len(self.items) + 1:02d}", "title": title, "sender_name": name or (card["contact"] if card else ""),
            "sender_address": address, "reply_to": reply_to, "links": [dict(link) for link in links],
            "subject": subject, "body": body,
            "expected": {"intent": intent, "promised_date": date, "installments": months, "client": expected_client,
                         "fraud": fraud, "accepted_intents": accepted_intents or [intent],
                         "action": action, "accepted_actions": accepted_actions or [action]},
        })


def build_replies() -> list[dict]:
    b = Builder()

    # Paiement annoncé, sans date précise (7)
    b.add("Virement parti", 0, "Bonjour,\n\nLe virement est parti ce matin, vous devriez le recevoir sous peu.\n\nPaul Girard", "paiement_annonce")
    b.add("Réglé hier", 1, "Bonjour,\n\nNous avons réglé cette facture hier par virement.\n\nJulien", "paiement_annonce", via="expediteur")
    b.add("Chèque posté", 2, "Bonjour, le chèque a été posté lundi, il devrait vous parvenir cette semaine. Marc Petit", "paiement_annonce")
    b.add("Fin du mois (vague)", 3, "Bonjour,\n\nJe règle ça fin du mois sans faute.\n\nNadia Benali", "paiement_annonce",
          accepted_intents=["paiement_annonce", "promesse_date"], accepted_actions=["report", "humain"])
    b.add("Dès que possible (vague)", 4, "Bonjour, je fais le virement dès que possible, merci de votre patience. E. Garnier", "paiement_annonce", via="expediteur")
    b.add("Paiement en anglais", 5, "Hello,\n\nI have paid this invoice by bank transfer yesterday, please check your account.\n\nAnne", "paiement_annonce")
    b.add("Paiement en cours", 7, "Bonjour,\n\nLe paiement est en cours de traitement par notre comptabilité.\n\nClaire Martin", "paiement_annonce")

    # Promesse de date (9)
    b.add("30 octobre", 1, "Bonjour,\n\nJe vous règle le 30 octobre.\n\nJulien Roche", "promesse_date", date="2026-10-30")
    b.add("Date en toutes lettres", 0, "Bonjour,\n\nLe paiement sera effectué le trente novembre.\n\nPaul Girard", "promesse_date", date="2026-11-30")
    b.add("5 novembre", 2, "Bonjour,\n\nVirement prévu le 5 novembre prochain.\n\nMarc Petit", "promesse_date", date="2026-11-05")
    b.add("Le 30 (mois courant)", 3, "Bonjour, nous paierons le 30. Merci de votre compréhension. Nadia Benali", "promesse_date", date="2026-10-30", via="expediteur")
    b.add("2 novembre 2026", 4, "Bonjour,\n\nLe règlement partira le 2 novembre 2026.\n\nÉlodie Garnier", "promesse_date", date="2026-11-02")
    b.add("Vendredi 23 octobre", 5, "Bonjour,\n\nJe vous paie le vendredi 23 octobre.\n\nAnne Roux", "promesse_date", date="2026-10-23")
    b.add("Promesse en anglais", 6, "Hello,\n\nWe will pay on November 12th.\n\nSophie Leroy", "promesse_date", date="2026-11-12")
    b.add("Date passée", 7, "Bonjour,\n\nJe vous règle le 3 octobre au plus tard.\n\nClaire Martin", "promesse_date", date=None)
    b.add("15 novembre", 0, "Bonjour,\n\nLe paiement interviendra le 15 novembre.\n\nPaul Girard", "promesse_date", date="2026-11-15", via="autre_adresse", sender="comptabilite@groupe-hotelier.fr", name="Service comptabilité")

    # Demande d'échéancier (7)
    b.add("Échéancier demandé en 6 fois", 0, "Bonjour,\n\nPourrions-nous régler cette facture en 6 fois ?\n\nMarc Petit", "demande_echeancier", months=6)
    b.add("Trois mensualités", 1, "Bonjour,\n\nJe souhaiterais régler en trois mensualités.\n\nNadia Benali", "demande_echeancier", months=3)
    b.add("Plusieurs mois, sans nombre", 6, "Bonjour,\n\nSerait-il possible d'étaler le paiement sur plusieurs mois ?\n\nÉlodie Garnier", "demande_echeancier", via="expediteur")
    b.add("Échéancier en anglais", 0, "Hello,\n\nCan we pay in 4 instalments?\n\nAnne", "demande_echeancier", months=4)
    b.add("Deux fois", 1, "Bonjour,\n\nNotre trésorerie est tendue : un paiement en deux fois est-il possible ?\n\nJulien", "demande_echeancier", months=2)
    b.add("Cinq mensualités", 1, "Bonjour,\n\nNous aimerions un échéancier, 5 mensualités si possible.\n\nClaire Martin", "demande_echeancier", months=5)
    b.add("En 3 fois", 6, "Bonjour,\n\nPeut-on payer en 3 fois ?\n\nSophie Leroy", "demande_echeancier", months=3, via="expediteur")

    # Accord sur un échéancier (6)
    b.add("Accord simple", 2, "Bonjour,\n\nNous acceptons l'échéancier proposé, merci.\n\nMarc Petit", "accord_echeancier", kind="E")
    b.add("Accord trois mensualités", 3, "Bonjour,\n\nD'accord pour les trois mensualités.\n\nNadia Benali", "accord_echeancier", kind="E")
    b.add("Je valide le plan", 4, "Bonjour,\n\nC'est bon pour moi, je valide le plan de paiement.\n\nÉlodie Garnier", "accord_echeancier", kind="E")
    b.add("Accord et dates", 5, "Bonjour,\n\nAccord reçu, nous réglerons chaque mensualité à la date prévue.\n\nAnne Roux", "accord_echeancier", kind="E")
    b.add("Accord en anglais", 7, "Hello,\n\nWe agree with the payment schedule you proposed.\n\nClaire Martin", "accord_echeancier", kind="E")
    b.add("Accord sans proposition", 1, "Bonjour,\n\nOk, d'accord pour votre proposition.\n\nJulien Roche", "accord_echeancier")

    # Contestation (6)
    b.add("Marchandise jamais reçue", 0, "Bonjour,\n\nNous n'avons jamais reçu cette marchandise : la facture est contestée.\n\nPaul Girard", "contestation")
    b.add("Polie mais contestataire", 1, "Bonjour,\n\nMerci pour votre message. Nous réglerons volontiers dès que le montant sera rectifié : il comprend des prestations qui n'ont pas été réalisées.\n\nCordialement, Julien Roche", "contestation")
    b.add("Montant différent du devis", 2, "Bonjour,\n\nLe montant ne correspond pas au devis signé (1 100 € au lieu de 1 180,40 €).\n\nMarc Petit", "contestation", via="expediteur")
    b.add("TVA appliquée à tort", 3, "Bonjour,\n\nIl y a une erreur sur la facture : la TVA a été appliquée à tort.\n\nNadia Benali", "contestation")
    b.add("Contestation en anglais", 5, "Hello,\n\nWe dispute this invoice: the goods arrived damaged.\n\nAnne", "contestation")
    b.add("Attente d'un avoir", 6, "Bonjour,\n\nNous attendons un avoir pour la livraison défectueuse avant tout paiement.\n\nSophie Leroy", "contestation")

    # Autre (6)
    b.add("Demande de duplicata", 4, "Bonjour,\n\nPouvez-vous me renvoyer un exemplaire de la facture ? Je ne la retrouve pas.\n\nÉlodie Garnier", "autre")
    b.add("Accusé de réception", 0, "Bonjour,\n\nAccusé de réception, nous transmettons à la comptabilité.\n\nPaul Girard", "autre")
    b.add("Absent jusqu'au 2 novembre", 7, "Bonjour,\n\nJe suis absent jusqu'au 2 novembre, mon collègue vous répondra à mon retour.\n\nClaire Martin", "autre")
    b.add("Question sur le SIRET", 3, "Bonjour,\n\nQuel est votre numéro de SIRET pour notre dossier fournisseur ?\n\nNadia Benali", "autre", via="expediteur")
    b.add("Réponse automatique", 5, "Hello,\n\nI am out of the office and will reply when I am back.\n\nAnne", "autre")
    b.add("Expéditeur inconnu", None, "Bonjour,\n\nNous proposons nos services de nettoyage de bureaux.\n\nSociété Propre", "autre", via="inconnu", sender="contact@societe-propre.fr", name="Société Propre")

    # Réponses frauduleuses : toujours « autre », aucune action (5)
    b.add("Domaine sosie et nouveau RIB", 4, "Bonjour,\n\nSuite à un changement de banque, merci de noter notre nouveau RIB pour tout avoir : IBAN FR76 3000 6000 0112 3456 7890 189.\n\nÉlodie Garnier", "autre", fraud=True, sender="e.garnier@maison-garnier.com")
    b.add("Réponse détournée et IBAN", 5, "Bonjour,\n\nPour tout remboursement, utilisez cet IBAN : FR76 1234 5678 9012 3456 7890 123.\n\nAnne Roux", "autre", fraud=True, reply_to="paiements@anne-cafe.example")
    b.add("Lien trompeur", 3, "Bonjour,\n\nTélécharger la facture rectifiée.\n\nNadia Benali", "autre", fraud=True, links=[{"text": "www.cabinet-benali.fr/facture", "url": "https://paiement-secure.example/x"}])
    b.add("Messagerie gratuite et nouveau RIB", 2, "Bonjour,\n\nNotre nouveau RIB pour tout avoir sera communiqué par ce message : IBAN FR76 3000 6000 0112 3456 7890 189.\n\nService comptabilité", "autre", fraud=True, sender="comptabilite.galerie.nova@gmail.com", name="Service Comptabilité Galerie Nova")
    b.add("Domaine sosie en anglais", 7, "Hello,\n\nNew bank details for any refund: IBAN FR76 3000 6000 0112 3456 7890 189.\n\nClaire", "autre", fraud=True, sender="compta@librairie-du-parc.com")

    # Réponse non rattachée et frauduleuse : fausse référence, domaine sosie d'un client, nouveau RIB (1)
    b.add("Non rattachée, domaine sosie", None, "Bonjour,\n\nSuite à un changement de banque, merci de régler sur notre nouveau RIB : IBAN FR76 3000 6000 0112 3456 7890 189.\n\nPaul Girard", "autre", fraud=True, via="inconnu", sender="compta@hotel-canut.fr", name="Paul Girard", subject="Re: Relance de paiement [Réf. R-99999]")

    # Rattachement : référence inconnue, retombée sur l'adresse de la fiche (1)
    b.add("Référence inconnue, adresse connue", 6, "Bonjour,\n\nLe virement sera effectué le 20 novembre.\n\nSophie Leroy", "promesse_date", date="2026-11-20", via="reference_inconnue")
    return b.items


def generate(path: Path = DATASET) -> list[dict]:
    replies = build_replies()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(replies, ensure_ascii=False, indent=2))
    return replies


if __name__ == "__main__":
    print(f"{len(generate())} réponses écrites dans {DATASET}")
