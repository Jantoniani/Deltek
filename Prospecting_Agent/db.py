"""SQLite persistence layer.

All database reads and writes live here. The rest of the app treats this
module as the only thing that knows SQL. Timestamps are stored as ISO-8601
UTC strings so they sort lexicographically and compare correctly.

Typical lifecycle of one search:
    init_db()                         # once, at startup
    get_cached_session(rep, company)  # short-circuit if fresh result exists
    get_search_history(rep, company)  # "you researched this N days ago"
    get_seen_articles(rep, company)   # URLs to exclude from the new search
    save_session(...)                 # persist session + triggers + seen URLs
    update_feedback(...)              # thumbs + optional text
"""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Serialize a datetime to an ISO-8601 UTC string (seconds precision)."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def url_hash(url: str) -> str:
    """Stable hash used for the (rep, company, url) uniqueness constraint."""
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    """Normalize rep/company keys so dedup and cache lookups are consistent."""
    return (value or "").strip().lower()


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def _db(db_path: str | None = None):
    """Open a connection, commit on success, and always close it.

    Plain `with sqlite3.connect(...) as conn` commits but never closes; this
    wrapper closes too, so we don't leak handles or risk write locks.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    """Create tables/indexes if they don't exist. Safe to call repeatedly."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with _db(db_path) as conn:
        conn.executescript(schema)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def get_search_history(rep_name: str, company: str, db_path: str | None = None) -> dict | None:
    """Return metadata for the most recent prior session for this rep+company.

    Used to render the "you researched {company} {N} days ago" banner. Returns
    None if this rep has never researched this company before.
    """
    with _db(db_path) as conn:
        row = conn.execute(
            """
            SELECT id, created_at, days_back
            FROM sessions
            WHERE lower(rep_name) = ? AND lower(company) = ?
            ORDER BY datetime(created_at) DESC
            LIMIT 1
            """,
            (_normalize(rep_name), _normalize(company)),
        ).fetchone()
    return dict(row) if row else None


def get_seen_articles(rep_name: str, company: str, db_path: str | None = None) -> set[str]:
    """Return the set of URLs already surfaced to this rep for this company."""
    with _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT url FROM articles_seen
            WHERE lower(rep_name) = ? AND lower(company) = ?
            """,
            (_normalize(rep_name), _normalize(company)),
        ).fetchall()
    return {row["url"] for row in rows}


def get_cached_session(rep_name: str, company: str, db_path: str | None = None) -> dict | None:
    """Return a still-fresh cached session (cached_until > now) plus its triggers.

    Returns None if no unexpired cached session exists. The returned dict matches
    the shape produced by `save_session` so the UI can render it identically to a
    fresh result.
    """
    with _db(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM sessions
            WHERE lower(rep_name) = ? AND lower(company) = ?
              AND cached_until IS NOT NULL
              AND datetime(cached_until) > datetime(?)
            ORDER BY datetime(created_at) DESC
            LIMIT 1
            """,
            (_normalize(rep_name), _normalize(company), _iso(_now())),
        ).fetchone()
        if not row:
            return None
        session = dict(row)
        session["triggers"] = _get_session_triggers(conn, session["id"])
    return session


def get_session(session_id: int, db_path: str | None = None) -> dict | None:
    """Fetch a session and its triggers by id."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        session = dict(row)
        session["triggers"] = _get_session_triggers(conn, session_id)
    return session


def _get_session_triggers(conn: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, trigger_type, headline, summary, source_url, confidence, rank
        FROM triggers
        WHERE session_id = ?
        ORDER BY rank ASC, id ASC
        """,
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def save_session(
    rep_name: str,
    company: str,
    contact: str | None,
    product_fit: str | None,
    days_back: int,
    company_snapshot: str | None,
    draft_email: str | None,
    triggers: list[dict],
    seen_urls: list[str],
    error: str | None = None,
    cache_hours: int | None = None,
    db_path: str | None = None,
) -> dict:
    """Persist a complete search session.

    Writes the session row, its ranked triggers, and marks every candidate URL
    as seen (whether or not it classified as a trigger). Returns a dict shaped
    like `get_cached_session` output, including the new session id and triggers
    with their assigned database ids (so feedback can target them).

    `triggers` is the ranked list of trigger dicts from `agent.rank_triggers`,
    each with keys: trigger_type, headline, summary, source_url, confidence,
    and (optionally) rank.
    """
    if cache_hours is None:
        cache_hours = config.CACHE_HOURS

    now = _now()
    cached_until = now + timedelta(hours=cache_hours)

    with _db(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO sessions
                (rep_name, company, contact, product_fit, days_back,
                 created_at, cached_until, company_snapshot, draft_email, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rep_name.strip(),
                company.strip(),
                (contact or "").strip() or None,
                product_fit,
                days_back,
                _iso(now),
                _iso(cached_until),
                company_snapshot,
                draft_email,
                error,
            ),
        )
        session_id = cur.lastrowid

        saved_triggers: list[dict] = []
        for idx, t in enumerate(triggers):
            rank = t.get("rank", idx + 1)
            tcur = conn.execute(
                """
                INSERT INTO triggers
                    (session_id, trigger_type, headline, summary, source_url, confidence, rank)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    t["trigger_type"],
                    t["headline"],
                    t["summary"],
                    t["source_url"],
                    t["confidence"],
                    rank,
                ),
            )
            saved = dict(t)
            saved["id"] = tcur.lastrowid
            saved["rank"] = rank
            saved_triggers.append(saved)

        _mark_articles_seen(conn, rep_name, company, seen_urls, now)
        conn.commit()

    return {
        "id": session_id,
        "rep_name": rep_name.strip(),
        "company": company.strip(),
        "contact": (contact or "").strip() or None,
        "product_fit": product_fit,
        "days_back": days_back,
        "created_at": _iso(now),
        "cached_until": _iso(cached_until),
        "company_snapshot": company_snapshot,
        "draft_email": draft_email,
        "error": error,
        "triggers": saved_triggers,
    }


def _mark_articles_seen(
    conn: sqlite3.Connection,
    rep_name: str,
    company: str,
    urls: list[str],
    now: datetime,
) -> None:
    """Insert URLs into articles_seen, ignoring duplicates (per the UNIQUE key)."""
    for url in urls:
        if not url:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO articles_seen
                (rep_name, company, url, url_hash, first_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rep_name.strip(), company.strip(), url, url_hash(url), _iso(now)),
        )


def update_feedback(
    session_id: int,
    target_type: str,
    target_id: int | None,
    rating: str,
    feedback_text: str | None = None,
    db_path: str | None = None,
) -> None:
    """Upsert a thumbs rating (+ optional text) for a trigger or the draft.

    target_type is 'trigger' (with target_id = trigger.id) or 'draft' (target_id
    None). Re-rating the same target overwrites the prior rating rather than
    appending, so each rep choice maps to one row.
    """
    if target_type not in ("trigger", "draft"):
        raise ValueError(f"invalid target_type: {target_type!r}")
    if rating not in ("up", "down"):
        raise ValueError(f"invalid rating: {rating!r}")

    now = _iso(_now())
    with _db(db_path) as conn:
        # Match NULL target_id explicitly (SQL '=' never matches NULL).
        if target_id is None:
            existing = conn.execute(
                """
                SELECT id FROM feedback
                WHERE session_id = ? AND target_type = ? AND target_id IS NULL
                """,
                (session_id, target_type),
            ).fetchone()
        else:
            existing = conn.execute(
                """
                SELECT id FROM feedback
                WHERE session_id = ? AND target_type = ? AND target_id = ?
                """,
                (session_id, target_type, target_id),
            ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE feedback
                SET rating = ?, feedback_text = ?, created_at = ?
                WHERE id = ?
                """,
                (rating, feedback_text, now, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO feedback
                    (session_id, target_type, target_id, rating, feedback_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, target_type, target_id, rating, feedback_text, now),
            )
        conn.commit()


def get_feedback(session_id: int, db_path: str | None = None) -> list[dict]:
    """Return all feedback rows for a session (used by the UI to show current state)."""
    with _db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT target_type, target_id, rating, feedback_text, created_at
            FROM feedback WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]
