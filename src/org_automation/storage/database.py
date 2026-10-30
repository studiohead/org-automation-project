"""PLAIN ENGLISH: The filing cabinet.

A small local database (SQLite — just one file) with these drawers:
  processed_events  – "have we already handled this request?" (stops double-processing)
  audit_events      – the permanent, tamper-evident diary of everything that happened
  approvals         – the to-do list for human reviewers, plus their decisions
  knowledge_documents / kb_parents / kb_children / kb_children_fts – the searchable policy library
                      (see knowledge/). Needs SQLite's built-in full-text search (FTS5), included in standard Python.
  mock_archive      – stand-in for "records filed in SharePoint" (demo only)
  mock_crm_updates  – stand-in for "changes written to the CRM" so the demo can show the full loop
In production this would be a managed database (e.g. Azure SQL / Postgres); the code shape stays the same.
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from org_automation.core.settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_events(
    idempotency_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, document_id TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL, document_id TEXT NOT NULL, actor TEXT NOT NULL, outcome TEXT NOT NULL,
    details_json TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
    approval_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, kind TEXT NOT NULL,
    proposal_json TEXT NOT NULL, proposal_hash TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, submitted_by TEXT NOT NULL DEFAULT 'system',
    decided_by TEXT, decided_at TEXT, decision_note TEXT);
CREATE TABLE IF NOT EXISTS knowledge_documents(
    document_id TEXT PRIMARY KEY, title TEXT NOT NULL, audience TEXT NOT NULL,
    content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_parents(
    parent_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    section_title TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_children(
    child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, document_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_embeddings(
    child_id TEXT PRIMARY KEY, model TEXT NOT NULL, vector_json TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_children_fts USING fts5(
    search_text, child_id UNINDEXED, document_id UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS mock_archive(
    file_name TEXT PRIMARY KEY, document_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mock_crm_updates(
    idempotency_key TEXT PRIMARY KEY, record_id TEXT NOT NULL, changes_json TEXT NOT NULL,
    applied_at TEXT NOT NULL);
"""


_initialised: set[str] = set()   # database files whose tables we have already checked in this process
_init_lock = threading.Lock()


def ensure_db() -> None:
    """Create the database file and tables if they don't exist yet (safe to call repeatedly).

    The check runs once per database file per process, not on every query, and switches SQLite to write-ahead
    logging so the web request, the background webhook task and reviewers can read while one of them writes."""
    path = Path(settings.database_path)
    key = str(path.resolve())
    if key in _initialised and path.exists():
        return
    with _init_lock:
        if str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()
        _initialised.add(key)


@contextmanager
def connect():
    """Open the database, hand it over, save (commit) if all went well, always close it."""
    ensure_db()
    db = sqlite3.connect(settings.database_path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()  # if anything failed halfway, undo the half-finished changes
        raise
    finally:
        db.close()
