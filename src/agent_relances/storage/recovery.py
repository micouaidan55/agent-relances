"""Réponses de clients, pauses, échéanciers et dossiers de contentieux. Une décision n'est jamais écrasée."""

import sqlite3
from datetime import date
from decimal import Decimal

from agent_relances.models import (
    ActionKind, Installment, Plan, PlanStatus, ReplyIntent, ReplyStatus, client_key,
    client_key as client_key_fn,
)
from agent_relances.rules.installments import installment_dates, plan_violations

_OPEN_PLAN = (PlanStatus.PROPOSED.value, PlanStatus.ACTIVE.value)
_HOLD_KINDS = ("report", "suspension")


class RecoveryStore:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # --- Réponses des clients ---

    def known_reply_ids(self) -> set[str]:
        return {row["message_id"] for row in self._conn.execute("SELECT message_id FROM replies")}

    def add_reply(
        self, *, message_id: str, client_key: str | None, reminder_ref: str | None, match_method: str, sender: str,
        subject: str, received_at: str, body: str, intent: ReplyIntent, promised_date: date | None,
        requested_installments: int | None, summary: str, suspicious: bool, proposed_action: ActionKind,
        action_until: date | None, status: ReplyStatus, cost_usd: float, today: date,
    ) -> int:
        """Enregistre une réponse ; un message déjà connu (même `message_id`) n'est jamais enregistré deux fois."""
        client_key_n = client_key_fn(client_key) if client_key else None
        if message_id in self.known_reply_ids():
            raise ValueError(f"Réponse déjà enregistrée : {message_id}")
        cursor = self._conn.execute(
            "INSERT INTO replies (message_id, client_key, reminder_ref, match_method, sender, subject,"
            " received_at, body, intent, promised_date, requested_installments, summary, suspicious, proposed_action,"
            " action_until, status, cost_usd, created_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, client_key_n, reminder_ref, match_method, sender, subject, received_at, body, intent.value,
             _iso(promised_date), requested_installments, summary, int(suspicious), proposed_action.value,
             _iso(action_until), status.value, cost_usd, today.isoformat()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def get_reply(self, reply_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone()
        return self._reply(row) if row else None

    def list_replies(self, statuses: list[ReplyStatus] | None = None) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM replies ORDER BY id DESC").fetchall()
        wanted = {s.value for s in statuses} if statuses is not None else None
        return [self._reply(r) for r in rows if wanted is None or r["status"] in wanted]

    def set_reply_status(self, reply_id: int, status: ReplyStatus) -> bool:
        """Décision atomique : seule une réponse « à traiter » peut changer de statut."""
        cursor = self._conn.execute(
            "UPDATE replies SET status = ? WHERE id = ? AND status = ?",
            (status.value, reply_id, ReplyStatus.TO_REVIEW.value),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def reopen_reply(self, reply_id: int) -> bool:
        """Annule une application qui a échoué : « appliquée » redevient « à traiter » (seulement depuis « appliquée »)."""
        cursor = self._conn.execute(
            "UPDATE replies SET status = ? WHERE id = ? AND status = ?",
            (ReplyStatus.TO_REVIEW.value, reply_id, ReplyStatus.APPLIED.value),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _reply(row: sqlite3.Row) -> dict:
        reply = dict(row)
        reply["intent"] = ReplyIntent(reply["intent"])
        reply["proposed_action"] = ActionKind(reply["proposed_action"])
        reply["status"] = ReplyStatus(reply["status"])
        reply["suspicious"] = bool(reply["suspicious"])
        for field in ("promised_date", "action_until", "created_on"):
            reply[field] = _date(reply[field])
        return reply

    # --- Pauses et suspensions ---

    def add_hold(self, client_key_: str, kind: str, until: date | None, reason: str, today: date) -> int:
        if kind not in _HOLD_KINDS:
            raise ValueError(f"Type de pause inconnu : {kind}")
        if kind == "report" and until is None:
            raise ValueError("Un report a une date de fin ; seule une suspension n'en a pas.")
        cursor = self._conn.execute(
            "INSERT INTO holds (client_key, kind, until, reason, created_on) VALUES (?, ?, ?, ?, ?)",
            (client_key(client_key_), kind, _iso(until), reason, today.isoformat()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def active_holds(self, today: date) -> dict[str, date | None]:
        """Client -> fin de la pause (`None` : suspension sans fin). Une pause expirée n'est pas active."""
        holds: dict[str, date | None] = {}
        rows = self._conn.execute("SELECT client_key, until FROM holds WHERE released_on IS NULL").fetchall()
        for row in rows:
            until = _date(row["until"])
            if until is not None and until <= today:
                continue
            if row["client_key"] in holds and (holds[row["client_key"]] is None or until is None):
                holds[row["client_key"]] = None
            elif row["client_key"] in holds:
                holds[row["client_key"]] = max(holds[row["client_key"]], until)
            else:
                holds[row["client_key"]] = until
        return holds

    def release_hold(self, hold_id: int, today: date) -> bool:
        cursor = self._conn.execute(
            "UPDATE holds SET released_on = ? WHERE id = ? AND released_on IS NULL", (today.isoformat(), hold_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    # --- Échéanciers ---

    def create_plan(
        self, client_key_: str, invoice_ids: list[int], total: Decimal, amounts: list[Decimal], today: date,
    ) -> int:
        problems = plan_violations(amounts, total)
        if problems:
            raise ValueError("Échéancier non conforme : " + " ".join(problems))
        if not invoice_ids:
            raise ValueError("Un échéancier couvre au moins une facture.")
        key = client_key(client_key_)
        if self.open_plan_for(key) is not None:
            raise ValueError("Un échéancier est déjà proposé ou en cours pour ce client.")
        try:
            cursor = self._conn.execute(
                "INSERT INTO plans (client_key, total, status, proposed_on) VALUES (?, ?, ?, ?)",
                (key, str(total), PlanStatus.PROPOSED.value, today.isoformat()),
            )
            plan_id = int(cursor.lastrowid)
            self._conn.executemany(
                "INSERT INTO plan_invoices (plan_id, invoice_id) VALUES (?, ?)",
                [(plan_id, invoice_id) for invoice_id in sorted(set(invoice_ids))],
            )
            self._conn.executemany(
                "INSERT INTO installments (plan_id, number, amount) VALUES (?, ?, ?)",
                [(plan_id, n, str(amount)) for n, amount in enumerate(amounts, start=1)],
            )
        except sqlite3.IntegrityError as error:
            self._conn.rollback()
            raise ValueError("Un échéancier est déjà proposé ou en cours pour ce client.") from error
        self._conn.commit()
        return plan_id

    def get_plan(self, plan_id: int) -> Plan | None:
        row = self._conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return self._plan(row) if row else None

    def open_plan_for(self, client_key_: str) -> Plan | None:
        row = self._conn.execute(
            "SELECT * FROM plans WHERE client_key = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 1",
            (client_key(client_key_), *_OPEN_PLAN),
        ).fetchone()
        return self._plan(row) if row else None

    def list_plans(self, statuses: list[PlanStatus] | None = None) -> list[Plan]:
        rows = self._conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
        wanted = {s.value for s in statuses} if statuses is not None else None
        return [self._plan(r) for r in rows if wanted is None or r["status"] in wanted]

    def activate_plan(self, plan_id: int, agreed_on: date) -> bool:
        """Accord du client : fixe les dates (1re échéance 30 jours après), seulement depuis « proposé »."""
        count = self._conn.execute("SELECT COUNT(*) FROM installments WHERE plan_id = ?", (plan_id,)).fetchone()[0]
        cursor = self._conn.execute(
            "UPDATE plans SET status = ?, agreed_on = ? WHERE id = ? AND status = ?",
            (PlanStatus.ACTIVE.value, agreed_on.isoformat(), plan_id, PlanStatus.PROPOSED.value),
        )
        if cursor.rowcount != 1:
            self._conn.rollback()
            return False
        self._conn.executemany(
            "UPDATE installments SET due_date = ? WHERE plan_id = ? AND number = ?",
            [(d.isoformat(), plan_id, n) for n, d in enumerate(installment_dates(agreed_on, count), start=1)],
        )
        self._conn.commit()
        return True

    def mark_received(self, plan_id: int, number: int, on: date) -> bool:
        """Mensualité reçue : seulement dans un échéancier en cours, et une seule fois."""
        cursor = self._conn.execute(
            "UPDATE installments SET received_on = ? WHERE plan_id = ? AND number = ? AND received_on IS NULL"
            " AND EXISTS (SELECT 1 FROM plans WHERE id = ? AND status = ?)",
            (on.isoformat(), plan_id, number, plan_id, PlanStatus.ACTIVE.value),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def set_plan_status(self, plan_id: int, status: PlanStatus, today: date) -> bool:
        """Clôture : annulé depuis proposé ou en cours ; soldé ou rompu depuis en cours seulement."""
        if status is PlanStatus.CANCELLED:
            allowed = _OPEN_PLAN
        elif status in (PlanStatus.SETTLED, PlanStatus.BROKEN):
            allowed = (PlanStatus.ACTIVE.value,)
        else:
            return False
        marks = ", ".join("?" for _ in allowed)
        cursor = self._conn.execute(
            f"UPDATE plans SET status = ?, closed_on = ? WHERE id = ? AND status IN ({marks})",
            (status.value, today.isoformat(), plan_id, *allowed),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def planned_invoice_ids(self) -> set[int]:
        """Factures couvertes par un échéancier proposé ou en cours (sorties du cycle de relances)."""
        rows = self._conn.execute(
            "SELECT pi.invoice_id FROM plan_invoices pi JOIN plans p ON p.id = pi.plan_id WHERE p.status IN (?, ?)",
            _OPEN_PLAN,
        )
        return {row["invoice_id"] for row in rows}

    def _plan(self, row: sqlite3.Row) -> Plan:
        invoices = self._conn.execute(
            "SELECT invoice_id FROM plan_invoices WHERE plan_id = ? ORDER BY invoice_id", (row["id"],),
        )
        installments = self._conn.execute(
            "SELECT * FROM installments WHERE plan_id = ? ORDER BY number", (row["id"],),
        )
        return Plan(
            id=row["id"], client_key=row["client_key"], invoice_ids=[r["invoice_id"] for r in invoices],
            total=Decimal(row["total"]), status=PlanStatus(row["status"]), proposed_on=date.fromisoformat(row["proposed_on"]),
            agreed_on=_date(row["agreed_on"]),
            installments=[
                Installment(number=r["number"], amount=Decimal(r["amount"]), due_date=_date(r["due_date"]),
                            received_on=_date(r["received_on"]))
                for r in installments
            ],
        )

    # --- Relances de mensualités ---

    def record_installment_reminder(
        self, plan_id: int, number: int, stage: int, decided_on: date, eml_path: str | None,
    ) -> bool:
        """Première décision conservée : un palier déjà enregistré n'est jamais écrasé (retourne False)."""
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO installment_reminders (plan_id, number, stage, decided_on, eml_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (plan_id, number, stage, decided_on.isoformat(), eml_path),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def installment_reminders(self, plan_id: int) -> dict[int, list[tuple[int, date]]]:
        """Mensualité -> [(palier, date de validation)] dans l'ordre des paliers."""
        result: dict[int, list[tuple[int, date]]] = {}
        rows = self._conn.execute(
            "SELECT number, stage, decided_on FROM installment_reminders WHERE plan_id = ? ORDER BY number, stage",
            (plan_id,),
        )
        for row in rows:
            result.setdefault(row["number"], []).append((row["stage"], date.fromisoformat(row["decided_on"])))
        return result

    # --- Contentieux ---

    def add_dispute(self, client_key_: str, folder: str, total_due: Decimal, today: date) -> int:
        cursor = self._conn.execute(
            "INSERT INTO disputes (client_key, folder, created_on, total_due) VALUES (?, ?, ?, ?)",
            (client_key(client_key_), folder, today.isoformat(), str(total_due)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def open_dispute_records(
        self, client_key_: str, folder: str, total_due: Decimal, plan_id: int | None, plan_status: PlanStatus | None,
        today: date,
    ) -> int:
        """Ouverture d'un contentieux en une seule transaction : suspension sans fin, statut du plan, litige."""
        key = client_key(client_key_)
        try:
            if any(r["client_key"] == key for r in self._conn.execute("SELECT client_key FROM disputes")):
                raise ValueError("Un contentieux est déjà ouvert pour ce client.")
            self._conn.execute(
                "INSERT INTO holds (client_key, kind, until, reason, created_on) VALUES (?, 'suspension', NULL,"
                " 'contentieux', ?)", (key, today.isoformat()))
            if plan_id is not None and plan_status is not None:
                allowed = (PlanStatus.ACTIVE.value,) if plan_status is PlanStatus.BROKEN else _OPEN_PLAN
                cursor = self._conn.execute(
                    f"UPDATE plans SET status = ?, closed_on = ? WHERE id = ? AND status IN ({', '.join('?' for _ in allowed)})",
                    (plan_status.value, today.isoformat(), plan_id, *allowed))
                if cursor.rowcount != 1:
                    raise ValueError("Le statut de l'échéancier a changé : contentieux non ouvert.")
            cursor = self._conn.execute(
                "INSERT INTO disputes (client_key, folder, created_on, total_due) VALUES (?, ?, ?, ?)",
                (key, folder, today.isoformat(), str(total_due)))
            self._conn.commit()
            return int(cursor.lastrowid)
        except BaseException:
            self._conn.rollback()
            raise

    def list_disputes(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM disputes ORDER BY id DESC").fetchall()
        return [
            {**dict(r), "created_on": date.fromisoformat(r["created_on"]), "total_due": Decimal(r["total_due"])}
            for r in rows
        ]


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
