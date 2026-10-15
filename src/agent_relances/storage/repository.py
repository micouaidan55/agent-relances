"""Fiches clients, historique des relances et journal. Une décision humaine n'est jamais écrasée."""

import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel

from agent_relances.models import (
    Client, DraftSource, DueReminder, OverdueInvoice, PreparedReminder, Profile, ReminderDraft,
    ReminderStatus, Stage, Tone, client_key,
)
from agent_relances.rules.schedule import InvoiceState


class StoredReminder(BaseModel):
    id: int
    client_key: str
    client_name: str
    stage: Stage
    tone: Tone | None
    profile: Profile | None
    legal_notice: bool
    status: ReminderStatus
    subject: str
    body: str
    original_body: str
    source: DraftSource
    first_try_ok: bool
    problems: list[str]
    cost_usd: float
    seconds: float
    prepared_on: date
    decided_on: date | None
    postponed_until: date | None
    eml_path: str | None
    invoices: list[OverdueInvoice]
    invoice_stages: dict[int, int]

    @property
    def total(self) -> Decimal:
        return sum((invoice.amount for invoice in self.invoices), Decimal("0"))

    def to_due(self, client: Client) -> DueReminder:
        return DueReminder(
            client=client, profile=self.profile or Profile.OCCASIONAL, stage=self.stage, tone=self.tone or Tone.FIRM,
            invoices=self.invoices, invoice_stages=self.invoice_stages, legal_notice=self.legal_notice,
            today=self.prepared_on,
        )


class ReminderStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # --- Fiches clients ---

    def save_client(self, client: Client) -> None:
        self._conn.execute(
            "INSERT INTO clients (key, name, email, contact, notes, professional) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET name = excluded.name, email = excluded.email, contact = excluded.contact,"
            " notes = excluded.notes, professional = excluded.professional, updated_at = datetime('now')",
            (client.key, client.name.strip(), client.email.strip(), client.contact.strip(), client.notes.strip(),
             int(client.professional)),
        )
        self._conn.commit()

    def get_client(self, key: str) -> Client | None:
        row = self._conn.execute("SELECT * FROM clients WHERE key = ?", (client_key(key),)).fetchone()
        return self._client(row) if row else None

    def list_clients(self) -> list[Client]:
        return [self._client(row) for row in self._conn.execute("SELECT * FROM clients ORDER BY key")]

    def clients_by_key(self) -> dict[str, Client]:
        return {client.key: client for client in self.list_clients()}

    @staticmethod
    def _client(row) -> Client:
        return Client(name=row["name"], email=row["email"], contact=row["contact"], notes=row["notes"],
                      professional=bool(row["professional"]))

    # --- Relances ---

    def _insert(self, due: DueReminder, status: ReminderStatus, draft: ReminderDraft, source: DraftSource,
                first_try_ok: bool = True, problems: list[str] | None = None, cost_usd: float = 0.0,
                seconds: float = 0.0, decided_on: date | None = None, eml_path: str | None = None) -> int:
        cursor = self._conn.execute(
            "INSERT INTO reminders (client_key, client_name, stage, tone, profile, legal_notice, status, subject, body,"
            " original_body, source, first_try_ok, problems, cost_usd, seconds, prepared_on, decided_on, eml_path)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (due.client.key, due.client.name, int(due.stage), due.tone.value, due.profile.value, int(due.legal_notice),
             status.value, draft.subject, draft.body, draft.body, source.value, int(first_try_ok),
             json.dumps(problems or [], ensure_ascii=False), cost_usd, seconds, due.today.isoformat(),
             decided_on.isoformat() if decided_on else None, eml_path),
        )
        reminder_id = cursor.lastrowid
        self._insert_invoices(reminder_id, due.invoices, due.invoice_stages, int(due.stage))
        self._conn.commit()
        return reminder_id

    def _insert_invoices(self, reminder_id: int, invoices: list[OverdueInvoice], stages: dict[int, int],
                         default_stage: int) -> None:
        self._conn.executemany(
            "INSERT INTO reminder_invoices (reminder_id, invoice_id, number, customer, amount, issue_date, due_date,"
            " days_late, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(reminder_id, i.invoice_id, i.number, i.customer, str(i.amount), i.issue_date.isoformat(),
              i.due_date.isoformat(), i.days_late, stages.get(i.invoice_id, default_stage)) for i in invoices],
        )

    def add_prepared(self, prepared: PreparedReminder) -> int:
        return self._insert(prepared.due, ReminderStatus.TO_VALIDATE, prepared.draft, prepared.source,
                            prepared.first_try_ok, prepared.problems, prepared.usage.cost_usd, prepared.seconds)

    def add_validated(self, due: DueReminder, draft: ReminderDraft, decided_on: date,
                      source: DraftSource = DraftSource.MODEL, eml_path: str | None = None) -> int:
        return self._insert(due, ReminderStatus.VALIDATED, draft, source, decided_on=decided_on, eml_path=eml_path)

    def add_escalation(self, invoice: OverdueInvoice, today: date) -> int:
        cursor = self._conn.execute(
            "INSERT INTO reminders (client_key, client_name, stage, status, prepared_on, decided_on)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (client_key(invoice.customer), invoice.customer, int(Stage.FINAL), ReminderStatus.HUMAN.value,
             today.isoformat(), today.isoformat()),
        )
        self._insert_invoices(cursor.lastrowid, [invoice], {}, int(Stage.FINAL))
        self._conn.commit()
        return cursor.lastrowid

    def discard_pending(self) -> int:
        ids = [row["id"] for row in self._conn.execute(
            "SELECT id FROM reminders WHERE status = ?", (ReminderStatus.TO_VALIDATE.value,))]
        self._conn.executemany("DELETE FROM reminder_invoices WHERE reminder_id = ?", [(i,) for i in ids])
        self._conn.executemany("DELETE FROM reminders WHERE id = ?", [(i,) for i in ids])
        self._conn.commit()
        return len(ids)

    def get(self, reminder_id: int) -> StoredReminder | None:
        rows = self._select("WHERE id = ?", (reminder_id,))
        return rows[0] if rows else None

    def list_reminders(self, statuses: set[ReminderStatus] | None = None) -> list[StoredReminder]:
        if not statuses:
            return self._select("", ())
        marks = ", ".join("?" for _ in statuses)
        return self._select(f"WHERE status IN ({marks})", tuple(s.value for s in statuses))

    def _decide(self, reminder_id: int, status: ReminderStatus, today: date, extra: str = "", params: tuple = ()) -> bool:
        cursor = self._conn.execute(
            f"UPDATE reminders SET status = ?, decided_on = ?{extra} WHERE id = ? AND status = ?",
            (status.value, today.isoformat(), *params, reminder_id, ReminderStatus.TO_VALIDATE.value),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def validate(self, reminder_id: int, subject: str, body: str, eml_path: str, today: date) -> bool:
        return self._decide(reminder_id, ReminderStatus.VALIDATED, today, ", subject = ?, body = ?, eml_path = ?",
                            (subject, body, eml_path))

    def postpone(self, reminder_id: int, days: int, today: date) -> bool:
        until = today + timedelta(days=max(int(days), 1))
        return self._decide(reminder_id, ReminderStatus.POSTPONED, today, ", postponed_until = ?", (until.isoformat(),))

    def to_human(self, reminder_id: int, today: date) -> bool:
        return self._decide(reminder_id, ReminderStatus.HUMAN, today)

    def cancel(self, reminder_id: int, today: date) -> bool:
        return self._decide(reminder_id, ReminderStatus.CANCELLED, today)

    # --- État des factures, déduit de l'historique ---

    def _invoice_rows(self, status: ReminderStatus):
        return self._conn.execute(
            "SELECT ri.invoice_id, ri.stage, r.client_key, r.decided_on, r.postponed_until FROM reminder_invoices ri"
            " JOIN reminders r ON r.id = ri.reminder_id WHERE r.status = ?", (status.value,))

    def invoice_states(self) -> dict[int, InvoiceState]:
        last_stage: dict[int, int] = {}
        escalated: set[int] = set()
        postponed: dict[int, date] = {}
        for row in self._invoice_rows(ReminderStatus.VALIDATED):
            last_stage[row["invoice_id"]] = max(last_stage.get(row["invoice_id"], 0), row["stage"])
        for row in self._invoice_rows(ReminderStatus.HUMAN):
            escalated.add(row["invoice_id"])
        for row in self._invoice_rows(ReminderStatus.POSTPONED):
            until = date.fromisoformat(row["postponed_until"])
            postponed[row["invoice_id"]] = max(postponed.get(row["invoice_id"], until), until)
        return {
            invoice_id: InvoiceState(last_stage=last_stage.get(invoice_id, 0), escalated=invoice_id in escalated,
                                     postponed_until=postponed.get(invoice_id))
            for invoice_id in set(last_stage) | escalated | set(postponed)
        }

    def last_contacts(self) -> dict[str, date]:
        rows = self._conn.execute(
            "SELECT client_key, MAX(decided_on) AS last FROM reminders WHERE status = ? GROUP BY client_key",
            (ReminderStatus.VALIDATED.value,))
        return {row["client_key"]: date.fromisoformat(row["last"]) for row in rows}

    def first_validated(self) -> dict[int, date]:
        first: dict[int, date] = {}
        for row in self._invoice_rows(ReminderStatus.VALIDATED):
            decided = date.fromisoformat(row["decided_on"])
            first[row["invoice_id"]] = min(first.get(row["invoice_id"], decided), decided)
        return first

    # --- Journal ---

    def log(self, reminder_id: int | None, action: str, detail: dict | None = None) -> None:
        self._conn.execute("INSERT INTO action_log (reminder_id, action, detail) VALUES (?, ?, ?)",
                           (reminder_id, action, json.dumps(detail or {}, ensure_ascii=False, default=str)))
        self._conn.commit()

    def list_log(self, limit: int = 200) -> list[dict]:
        rows = self._conn.execute(
            "SELECT reminder_id, action, detail, created_at FROM action_log ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def _select(self, where: str, params: tuple) -> list[StoredReminder]:
        rows = self._conn.execute(f"SELECT * FROM reminders {where} ORDER BY id DESC", params).fetchall()
        result = []
        for row in rows:
            invoice_rows = self._conn.execute(
                "SELECT * FROM reminder_invoices WHERE reminder_id = ? ORDER BY due_date, number", (row["id"],)).fetchall()
            result.append(StoredReminder(
                id=row["id"], client_key=row["client_key"], client_name=row["client_name"], stage=row["stage"],
                tone=row["tone"], profile=row["profile"], legal_notice=bool(row["legal_notice"]), status=row["status"],
                subject=row["subject"], body=row["body"], original_body=row["original_body"], source=row["source"],
                first_try_ok=bool(row["first_try_ok"]), problems=json.loads(row["problems"]),
                cost_usd=row["cost_usd"], seconds=row["seconds"], prepared_on=row["prepared_on"],
                decided_on=row["decided_on"], postponed_until=row["postponed_until"], eml_path=row["eml_path"],
                invoices=[OverdueInvoice(invoice_id=i["invoice_id"], number=i["number"], customer=i["customer"],
                                         amount=Decimal(i["amount"]), issue_date=i["issue_date"],
                                         due_date=i["due_date"], days_late=i["days_late"]) for i in invoice_rows],
                invoice_stages={i["invoice_id"]: i["stage"] for i in invoice_rows},
            ))
        return result
