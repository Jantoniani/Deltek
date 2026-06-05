-- SQLite schema for the prospecting agent. Idempotent: safe to run on every
-- startup. SQLite has no native TIMESTAMP type; these columns store ISO-8601
-- strings (UTC) written by the application layer.

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rep_name TEXT NOT NULL,
    company TEXT NOT NULL,
    contact TEXT,
    product_fit TEXT,
    days_back INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cached_until TIMESTAMP,
    company_snapshot TEXT,
    draft_email TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS articles_seen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rep_name TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(rep_name, company, url_hash)
);

CREATE TABLE IF NOT EXISTS triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    trigger_type TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_url TEXT NOT NULL,
    confidence TEXT NOT NULL,
    rank INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    target_type TEXT NOT NULL,  -- 'trigger' or 'draft'
    target_id INTEGER,           -- trigger.id when target_type='trigger', null for draft
    rating TEXT NOT NULL,        -- 'up' or 'down'
    feedback_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- Lookup indexes for the hot paths: cache check, history, and dedup.
CREATE INDEX IF NOT EXISTS idx_sessions_rep_company
    ON sessions(rep_name, company, created_at);
CREATE INDEX IF NOT EXISTS idx_articles_seen_rep_company
    ON articles_seen(rep_name, company);
CREATE INDEX IF NOT EXISTS idx_triggers_session
    ON triggers(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_target
    ON feedback(session_id, target_type, target_id);
