"""Formats français partagés : montants et dates."""

from datetime import date

from agent_factures.tools.formatting import euros

__all__ = ["date_fr", "euros"]


def date_fr(value: date) -> str:
    return f"{value:%d/%m/%Y}"
