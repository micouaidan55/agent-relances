"""Mention légale de l'indemnité forfaitaire pour frais de recouvrement (professionnels uniquement)."""

from decimal import Decimal

from agent_relances.models import Stage

LEGAL_AMOUNT = Decimal("40")
LEGAL_MARKER = "indemnité forfaitaire"
LEGAL_NOTICE = (
    "Nous vous rappelons que tout retard de paiement entre professionnels entraîne de plein droit une "
    "indemnité forfaitaire pour frais de recouvrement de 40 € (article L441-10 du Code de commerce)."
)


def needs_legal_notice(stage: Stage, professional: bool) -> bool:
    return Stage(stage) is Stage.FINAL and professional
