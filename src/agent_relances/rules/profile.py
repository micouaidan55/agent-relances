"""Profil de paiement d'un client, calculé sur ses factures payées des 12 derniers mois."""

from datetime import date

from agent_relances.models import PaymentRecord, Profile

GOOD_MAX_DAYS = 5
OCCASIONAL_MAX_DAYS = 20
HISTORY_DAYS = 365


def client_profile(history: list[PaymentRecord], overdue_count: int, today: date) -> Profile:
    if overdue_count >= 2:
        return Profile.CHRONIC
    recent = [record for record in history if 0 <= (today - record.paid_at).days <= HISTORY_DAYS]
    if not recent:
        return Profile.OCCASIONAL
    mean = sum(record.days_late for record in recent) / len(recent)
    if mean <= GOOD_MAX_DAYS:
        return Profile.GOOD
    if mean <= OCCASIONAL_MAX_DAYS:
        return Profile.OCCASIONAL
    return Profile.CHRONIC
