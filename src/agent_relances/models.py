"""Modèles de données de l'agent de relance."""

import re
import unicodedata
from datetime import date
from decimal import Decimal
from email.utils import parseaddr
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


def client_key(name: str) -> str:
    """Clé de rapprochement entre le client d'une facture et sa fiche : casse, accents et espaces ignorés."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c))
    return " ".join(plain.casefold().split())


class Profile(StrEnum):
    GOOD = "bon_payeur"
    OCCASIONAL = "occasionnel"
    CHRONIC = "retardataire_chronique"


class Tone(StrEnum):
    LIGHT = "leger"
    NEUTRAL = "neutre"
    FIRM = "ferme"


class Stage(IntEnum):
    COURTEOUS = 1
    FIRM = 2
    FINAL = 3


STAGE_LABELS = {
    Stage.COURTEOUS: "Rappel courtois",
    Stage.FIRM: "Relance ferme",
    Stage.FINAL: "Dernier rappel avant mise en demeure",
}


class ReminderStatus(StrEnum):
    TO_VALIDATE = "a_valider"
    VALIDATED = "validee"
    POSTPONED = "reportee"
    HUMAN = "a_traiter_humain"
    CANCELLED = "annulee"


class DraftSource(StrEnum):
    MODEL = "modele"
    FALLBACK = "secours"


class Client(BaseModel):
    name: str = Field(min_length=1)
    email: str = ""
    contact: str = ""
    notes: str = ""
    professional: bool = True

    @property
    def key(self) -> str:
        return client_key(self.name)

    @property
    def complete(self) -> bool:
        name, address = parseaddr(self.email)
        local, _, domain = address.partition("@")
        return (not name and address == self.email.strip() and bool(local) and "." in domain
                and not domain.startswith(".") and not domain.endswith(".") and not re.search(r"[,;\s]", address))


class OverdueInvoice(BaseModel):
    invoice_id: int
    number: str
    customer: str
    amount: Decimal
    issue_date: date
    due_date: date
    days_late: int


class PaymentRecord(BaseModel):
    customer: str
    due_date: date
    paid_at: date

    @property
    def days_late(self) -> int:
        return max((self.paid_at - self.due_date).days, 0)


class DueReminder(BaseModel):
    client: Client
    profile: Profile
    stage: Stage
    tone: Tone
    invoices: list[OverdueInvoice]
    invoice_stages: dict[int, int] = Field(default_factory=dict)
    legal_notice: bool
    today: date

    @property
    def total(self) -> Decimal:
        return sum((invoice.amount for invoice in self.invoices), Decimal("0"))


class ReminderDraft(BaseModel):
    subject: str = Field(description="Objet du mail, court.")
    body: str = Field(description="Corps du mail, sans l'objet, signé au nom de l'entreprise.")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class PreparedReminder(BaseModel):
    due: DueReminder
    draft: ReminderDraft
    source: DraftSource
    first_try_ok: bool
    problems: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    seconds: float = 0.0
