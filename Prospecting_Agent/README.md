# Prospecting Agent (MVP)

An on-demand sales prospecting tool for Sales reps. A rep enters a company,
and the agent:

1. Searches for recent news (configurable lookback, default 30 days) via Exa
2. Classifies each article against a trigger taxonomy (Claude Haiku)
3. Writes a short company snapshot (Claude Haiku)
4. Drafts a personalized outreach email anchored on the top trigger (Claude Sonnet)
5. Remembers what each rep has already seen, so the same article is never shown twice
6. Captures thumbs-up/down feedback on each trigger and on the draft

It is a **deterministic pipeline** (no agent framework, no dynamic tool
selection): `search → classify → rank → snapshot → draft`. Drafts are
**copy-paste only** — nothing is ever sent automatically.

---

## Project layout

```
agent.py            Pure pipeline logic (UI-agnostic, importable from a notebook)
app.py              Streamlit UI (thin wrapper over agent.py + db.py)
db.py               All SQLite reads/writes
config.py           Trigger taxonomy, product positioning, model names, time windows
prompts/
  classify.txt      Trigger classification prompt
  snapshot.txt      Company snapshot prompt
  draft_email.txt   Email drafting prompt (with few-shot examples)
schema.sql          SQLite schema (idempotent)
requirements.txt
.env.example
.streamlit/secrets.toml.example
```

`prospecting.db` is created automatically on first run.

---

## Setup (local)

Requires **Python 3.11+**.

```bash
# 1. Clone, then create a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets — pick ONE of the following:

#    (a) .env  (simplest for local dev)
cp .env.example .env
#    then edit .env and paste your real EXA_API_KEY and ANTHROPIC_API_KEY

#    (b) Streamlit secrets
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
#    then edit .streamlit/secrets.toml

# 4. Run
streamlit run app.py
```

The app opens at http://localhost:8501. Enter your name in the sidebar, then a
company, and click **Research**.

You need two API keys:
- **Exa** — https://exa.ai (web search + content)
- **Anthropic** — https://console.anthropic.com (Claude)

---

## Deployment (Streamlit Community Cloud)

1. Push this repo to GitHub (the `.gitignore` already excludes secrets and the
   local `prospecting.db`).
2. Go to https://share.streamlit.io and create a new app pointing at this repo,
   with `app.py` as the entrypoint and Python 3.11+.
3. In the app's **Settings → Secrets**, paste:
   ```toml
   EXA_API_KEY = "..."
   ANTHROPIC_API_KEY = "..."
   ```
4. Deploy. The app bridges `st.secrets` into environment variables at startup,
   so no further configuration is needed.

> **Note on persistence:** Streamlit Community Cloud uses an ephemeral
> filesystem. `prospecting.db` survives normal reruns but is **reset whenever
> the app reboots or is redeployed**. That is acceptable for a 4-week pilot. For
> durable storage, point `PROSPECTING_DB_PATH` at a mounted volume or migrate
> `db.py` to a hosted database (a v2 concern).

---

## How to add a new trigger type

Edit `config.py`:

1. Add an entry to `TRIGGER_TAXONOMY`. The key is the machine name; the value is
   the description sent verbatim to the classifier — write it the way you'd brief
   a rep on what counts:
   ```python
   TRIGGER_TAXONOMY = {
       ...
       "regulatory_change": "New regulation, audit finding, or compliance mandate "
                            "materially affecting how the company operates",
   }
   ```
2. Add a matching human-readable label to `TRIGGER_LABELS` (used for the UI badge):
   ```python
   TRIGGER_LABELS = {
       ...
       "regulatory_change": "Regulatory change",
   }
   ```

That's it — the classification tool schema is generated from `TRIGGER_TAXONOMY`
at call time, so no prompt or code changes are required.

---

## How to change models

All model IDs live in `config.py`:

```python
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"   # article classification
SNAPSHOT_MODEL = "claude-haiku-4-5-20251001"   # company snapshot
DRAFT_MODEL    = "claude-sonnet-4-6"           # email drafting
```

Change the string to any valid Anthropic model ID and restart. The MVP
deliberately uses Haiku for the high-volume classification/snapshot work and
Sonnet only for the single drafting call per session. Do not move drafting to
Opus unless pilot feedback shows Sonnet is insufficient.

Token budgets (`CLASSIFY_MAX_TOKENS`, `SNAPSHOT_MAX_TOKENS`, `DRAFT_MAX_TOKENS`)
and search/cache settings (`NEWS_RESULTS`, `CACHE_HOURS`, `DEFAULT_DAYS_BACK`,
etc.) are in the same file.

---

## How the pieces fit together

- **`agent.py` is UI-agnostic.** Every function can be called from a notebook:
  ```python
  import agent
  articles = agent.search_news("Acme Defense", days_back=30)
  triggers = agent.rank_triggers(agent.classify_triggers(articles, company="Acme Defense"))
  print(agent.research_company("Acme Defense"))
  ```
  It reads API keys from environment variables only.
- **`app.py`** loads `.env`, bridges `st.secrets` into the environment, and runs
  the pipeline only on Research/Refresh. All other interactions are cheap reruns
  off `st.session_state`.
- **`db.py`** is the single place that touches SQLite. Triggers and drafts are
  persisted *before* display, so a UI hiccup never loses work.

### Caching & dedup

- Results for a `(rep, company)` pair are cached for `CACHE_HOURS` (24h). A
  re-run within that window shows a yellow banner with a **Refresh** button to
  force a new search.
- Every candidate URL shown to a rep is recorded in `articles_seen`. Subsequent
  searches for the same `(rep, company)` exclude those URLs, so a rep only ever
  sees *new* triggers.

---

## Inspecting the data

Everything is in `prospecting.db` (SQLite). Useful for reviewing pilot feedback:

```bash
sqlite3 prospecting.db "SELECT rating, feedback_text FROM feedback WHERE target_type='draft';"
sqlite3 prospecting.db "SELECT trigger_type, confidence, headline FROM triggers;"
```

---

## Out of scope for this MVP

No auth beyond the name field, no Salesforce integration, no email sending, no
semantic dedup, no scheduled monitoring, no multi-rep dashboard, no production
observability. These are explicit v2 concerns.
