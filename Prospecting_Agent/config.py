"""Central configuration for the prospecting agent.

Everything that a non-engineer might reasonably want to tune lives here:
the trigger taxonomy, product positioning, which Claude model each step
uses, time windows, and cache duration. `agent.py` and `app.py` import
from this module rather than hard-coding any of these values.
"""

import os

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
# API keys are read from environment variables. `app.py` bridges Streamlit
# secrets (st.secrets) into os.environ at startup, and `.env` is loaded via
# python-dotenv for local development. Keeping the lookup here means agent.py
# never has to know whether it's running under Streamlit or a notebook.


def get_secret(name: str) -> str | None:
    """Return a secret value from the environment, or None if unset."""
    value = os.getenv(name)
    return value.strip() if value else None


# ---------------------------------------------------------------------------
# Models  (see README "How to change models")
# ---------------------------------------------------------------------------
# Cheapest-capable model per step. Classification and snapshot summarization
# are well within Haiku's range; only the single drafting call uses Sonnet.
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"
SNAPSHOT_MODEL = "claude-haiku-4-5-20251001"
DRAFT_MODEL = "claude-sonnet-4-6"

# Max tokens per call. Classification returns compact JSON; drafts are short.
CLASSIFY_MAX_TOKENS = 2048
SNAPSHOT_MAX_TOKENS = 400
DRAFT_MAX_TOKENS = 800

# ---------------------------------------------------------------------------
# Search / time windows
# ---------------------------------------------------------------------------
DEFAULT_DAYS_BACK = 30
MIN_DAYS_BACK = 7
MAX_DAYS_BACK = 90

# How many candidate articles to pull from Exa for trigger detection, and how
# many results to use for the company snapshot.
NEWS_RESULTS = 15
SNAPSHOT_RESULTS = 5

# Character budget for Exa excerpts sent to Claude for classification. Keeps
# the classification prompt cheap (excerpts + titles, never full pages).
EXCERPT_MAX_CHARS = 1000

# Full-page character budget when fetching the single top-trigger article for
# the drafting call.
FULL_CONTENT_MAX_CHARS = 4000

# How many triggers to surface in the primary results list before collapsing
# the rest into an expander.
TOP_TRIGGERS_DISPLAY = 5

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# A search result for a (rep, company) pair is reused for this many hours to
# avoid duplicate API spend on rapid re-runs. A Refresh button overrides it.
CACHE_HOURS = 24

# Small pause between Exa calls to stay under the 10 QPS rate limit.
EXA_RATE_LIMIT_SLEEP = 0.2

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("PROSPECTING_DB_PATH", "prospecting.db")

# ---------------------------------------------------------------------------
# Trigger taxonomy
# ---------------------------------------------------------------------------
# Add a new trigger type by adding a key/description here. The description is
# sent verbatim to the classifier, so write it the way you'd brief a rep.
TRIGGER_TAXONOMY = {
    "leadership_change": "New CFO, CIO, COO, Controller, VP Finance, VP IT, VP Operations, or equivalent C-suite/SVP role",
    "contract_award": "Federal contract award of $5M+ as prime contractor, or significant SLED/commercial contract win",
    "funding_event": "Series B+ equity round, PE buyout, growth equity, or significant debt facility ($25M+)",
    "ma_event": "Company acquired another firm, was acquired, or announced merger",
    "major_expansion": "New office, geographic expansion, vertical expansion (e.g., into federal), or major hiring surge",
    "other_significant_event": "Any other event a sales rep should know about that doesn't fit the above categories",
}

# Human-friendly labels for trigger type badges in the UI.
TRIGGER_LABELS = {
    "leadership_change": "Leadership change",
    "contract_award": "Contract award",
    "funding_event": "Funding event",
    "ma_event": "M&A event",
    "major_expansion": "Major expansion",
    "other_significant_event": "Other significant event",
}

# ---------------------------------------------------------------------------
# Product positioning
# ---------------------------------------------------------------------------
PRODUCT_POSITIONING = {
    "Costpoint": "ERP for government contractors. DCAA-compliant project accounting, indirect rate management, contract management. Best fit: federal contractors $50M+.",
    "Vantagepoint": "ERP for architecture, engineering, and consulting firms. Project-based accounting, resource planning, CRM. Best fit: A&E firms $25M+.",
    "Maconomy": "ERP for international professional services firms. Multi-currency, multi-entity. Best fit: global consulting and services firms.",
    "Ajera": "ERP for small-to-mid A&E firms. Project accounting and management. Best fit: A&E firms under $25M.",
    "GovWin IQ": "Market intelligence platform for government contractors. Identifies federal, state, local, and education opportunities years before RFP release, with contract awards data, agency intelligence, and competitive analysis. Best fit: any firm pursuing government contracts (federal, SLED, or both), from small businesses through large primes.",
    "Replicon": "Cloud-based time tracking and professional services automation. DCAA-compliant timesheets, project time and expense management, billing automation, and resource utilization. Best fit: services firms, consultancies, and government contractors of any size needing audit-ready time tracking.",
    "Other": "General Deltek positioning - project-based business software for project-driven organizations.",
}

# Order shown in the product-fit dropdown.
PRODUCT_OPTIONS = ["Other", "Costpoint", "Vantagepoint", "Maconomy", "Ajera", "GovWin IQ", "Replicon"]
