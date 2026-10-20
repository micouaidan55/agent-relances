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


class ReplyIntent(StrEnum):
    PAYMENT_ANNOUNCED = "paiement_annonce"
    PROMISED_DATE = "promesse_date"
    PLAN_REQUEST = "demande_echeancier"
    PLAN_AGREEMENT = "accord_echeancier"
    DISPUTE = "contestation"
    OTHER = "autre"


class ReplyReading(BaseModel):
    """Sortie structurée du modèle de compréhension d'une réponse de client."""

    intent: ReplyIntent
    promised_date: str | None = None  # AAAA-MM-JJ
    requested_installments: int | None = None
    invoice_numbers: list[str] = Field(default_factory=list)
    summary: str


class ReplyStatus(StrEnum):
    TO_REVIEW = "a_traiter"
    APPLIED = "appliquee"
    DISMISSED = "classee"
    UNMATCHED = "non_rattachee"


class ActionKind(StrEnum):
    POSTPONE = "report"
    SUSPEND = "suspension"
    PROPOSE_PLAN = "proposition_echeancier"
    ACTIVATE_PLAN = "activation_echeancier"
    HUMAN = "humain"
    NONE = "sans_objet"


class PlanStatus(StrEnum):
    PROPOSED = "propose"
    ACTIVE = "en_cours"
    SETTLED = "solde"
    BROKEN = "rompu"
    CANCELLED = "annule"


PLAN_STATUS_LABELS = {
    PlanStatus.PROPOSED: "Proposé", PlanStatus.ACTIVE: "En cours", PlanStatus.SETTLED: "Soldé",
    PlanStatus.BROKEN: "Rompu", PlanStatus.CANCELLED: "Annulé",
}


class Installment(BaseModel):
    number: int
    amount: Decimal
    due_date: date | None = None
    received_on: date | None = None


class Plan(BaseModel):
    id: int
    client_key: str
    invoice_ids: list[int]
    total: Decimal
    status: PlanStatus
    proposed_on: date
    agreed_on: date | None = None
    exception_reason: str | None = None   # dérogation humaine au plafond de 4 versements, motivée
    installments: list[Installment]

    @property
    def remaining(self) -> Decimal:
        received = sum((i.amount for i in self.installments if i.received_on is not None), Decimal("0"))
        return self.total - received
