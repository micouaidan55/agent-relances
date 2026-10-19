"""Connexion SQLite et schéma de la base d'agent-relances."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    professional INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    client_name TEXT NOT NULL,
    stage INTEGER NOT NULL,
    tone TEXT,
    profile TEXT,
    legal_notice INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    original_body TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'modele',
    first_try_ok INTEGER NOT NULL DEFAULT 1,
    problems TEXT NOT NULL DEFAULT '[]',
    cost_usd REAL NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    prepared_on TEXT NOT NULL,
    decided_on TEXT,
    postponed_until TEXT,
    eml_path TEXT,
    origin TEXT NOT NULL DEFAULT 'app'
);
CREATE TABLE IF NOT EXISTS reminder_invoices (
    reminder_id INTEGER NOT NULL,
    invoice_id INTEGER NOT NULL,
    number TEXT NOT NULL,
    customer TEXT NOT NULL,
    amount TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    days_late INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    PRIMARY KEY (reminder_id, invoice_id)
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    client_key TEXT,
    reminder_ref TEXT,
    match_method TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL,
    promised_date TEXT,
    requested_installments INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    suspicious INTEGER NOT NULL DEFAULT 0,
    proposed_action TEXT NOT NULL,
    action_until TEXT,
    status TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('report', 'suspension')),
    until TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_on TEXT NOT NULL,
    released_on TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    total TEXT NOT NULL,
    status TEXT NOT NULL,
    proposed_on TEXT NOT NULL,
    agreed_on TEXT,
    closed_on TEXT,
    exception_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_plan_per_client ON plans (client_key)
    WHERE status IN ('propose', 'en_cours');
CREATE TABLE IF NOT EXISTS plan_invoices (
    plan_id INTEGER NOT NULL,
    invoice_id INTEGER NOT NULL,
    PRIMARY KEY (plan_id, invoice_id)
);
CREATE TABLE IF NOT EXISTS installments (
    plan_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    amount TEXT NOT NULL,
    due_date TEXT,
    received_on TEXT,
    PRIMARY KEY (plan_id, number)
);
CREATE TABLE IF NOT EXISTS installment_reminders (
    plan_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    decided_on TEXT NOT NULL,
    eml_path TEXT,
    PRIMARY KEY (plan_id, number, stage)
);
CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key TEXT NOT NULL,
    folder TEXT NOT NULL,
    created_on TEXT NOT NULL,
    total_due TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_dispute_per_client ON disputes (client_key);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)")}
    if "origin" not in columns:  # bases créées avant l'ajout de la colonne
        conn.execute("ALTER TABLE reminders ADD COLUMN origin TEXT NOT NULL DEFAULT 'app'")
        conn.commit()
    plan_columns = {row["name"] for row in conn.execute("PRAGMA table_info(plans)")}
    if "exception_reason" not in plan_columns:  # bases créées avant la dérogation d'échéancier
        conn.execute("ALTER TABLE plans ADD COLUMN exception_reason TEXT")
        conn.commit()
    return conn
