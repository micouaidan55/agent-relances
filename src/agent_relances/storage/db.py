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
    eml_path TEXT
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
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
