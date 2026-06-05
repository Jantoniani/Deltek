"""Streamlit UI — thin presentation layer over agent.py and db.py.

Run with:  streamlit run app.py

This module does no business logic of its own. It collects input, calls the
deterministic pipeline in agent.py, persists results through db.py, and renders
them. The pipeline runs only when the rep clicks Research / Refresh; every other
interaction (feedback clicks, expanders) is a cheap rerun off session_state.
"""

import json
import os
from datetime import datetime, timezone

import streamlit as st
import streamlit.components.v1 as components

import agent
import config
import db

# ---------------------------------------------------------------------------
# Bootstrap: secrets + database
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv optional at runtime
    pass


def _bridge_streamlit_secrets() -> None:
    """Copy Streamlit Cloud secrets into os.environ so agent.py (which only
    knows about env vars) can read them. No-op locally where .env is used."""
    for key in ("EXA_API_KEY", "ANTHROPIC_API_KEY"):
        try:
            if key in st.secrets and not os.getenv(key):
                os.environ[key] = str(st.secrets[key])
        except Exception:
            # st.secrets raises if no secrets.toml exists; local .env covers that.
            pass


@st.cache_resource
def _init() -> bool:
    db.init_db()
    return True


# set_page_config must be the first Streamlit command on the page.
st.set_page_config(page_title="Deltek Prospecting Agent", page_icon=None, layout="centered")

_bridge_streamlit_secrets()
_init()

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
st.session_state.setdefault("rep_name", "")
st.session_state.setdefault("page", "input")
st.session_state.setdefault("results", None)
st.session_state.setdefault("feedback", {})  # {"trigger:<id>"|"draft": "up"|"down"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _days_since(iso_str: str):
    dt = _parse_iso(iso_str)
    if not dt:
        return None
    return max((datetime.now(timezone.utc) - dt).days, 0)


def _fmt_ts(iso_str: str) -> str:
    dt = _parse_iso(iso_str)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else str(iso_str)


def _fb_key(target_type: str, target_id) -> str:
    return f"{target_type}:{target_id}" if target_id is not None else "draft"


def copy_button(text: str, label: str = "Copy email") -> None:
    """A clipboard-copy button. Works over HTTPS (Streamlit Cloud); the editable
    text area above it is the fallback if the browser blocks clipboard access."""
    payload = json.dumps(text or "")
    lbl = json.dumps(label)
    components.html(
        f"""
        <button id="cpy" style="padding:0.45rem 0.9rem;border-radius:0.5rem;
            border:1px solid #cccccc;background:#f5f5f5;cursor:pointer;
            font-family:inherit;font-size:0.9rem;">{label}</button>
        <script>
        const b = document.getElementById('cpy');
        b.addEventListener('click', () => {{
            navigator.clipboard.writeText({payload}).then(() => {{
                b.innerText = 'Copied';
                setTimeout(() => b.innerText = {lbl}, 1500);
            }}).catch(() => {{ b.innerText = 'Press Ctrl+C to copy'; }});
        }});
        </script>
        """,
        height=46,
    )


# ---------------------------------------------------------------------------
# Pipeline (called only on Research / Refresh)
# ---------------------------------------------------------------------------
def run_pipeline(rep, company, contact, product_fit, days_back, refresh):
    if not refresh:
        cached = db.get_cached_session(rep, company)
        if cached:
            cached["from_cache"] = True
            cached["history_days"] = None
            return cached

    history = db.get_search_history(rep, company)
    history_days = _days_since(history["created_at"]) if history else None

    seen = db.get_seen_articles(rep, company)
    articles = agent.search_news(company, days_back, exclude_urls=seen)
    snapshot = agent.research_company(company)

    if not articles and not (snapshot or "").strip():
        raise agent.CompanyNotFoundError(
            f"Unable to find any information about {company}. Check the company "
            "name spelling, or try entering the domain instead."
        )

    triggers = agent.classify_triggers(articles, company=company) if articles else []
    triggers = agent.rank_triggers(triggers)

    top = triggers[0] if triggers else None
    draft = (
        agent.draft_email(top, snapshot, product_fit, rep, contact, company=company)
        if top
        else None
    )

    seen_urls = [a["url"] for a in articles]
    results = db.save_session(
        rep_name=rep,
        company=company,
        contact=contact,
        product_fit=product_fit,
        days_back=days_back,
        company_snapshot=snapshot,
        draft_email=draft,
        triggers=triggers,
        seen_urls=seen_urls,
    )
    results["from_cache"] = False
    results["history_days"] = history_days
    return results


def do_research(company, contact, product_fit, days_back, refresh=False):
    """Run the pipeline, handle errors, and transition to the results page.

    st.rerun() is deliberately called OUTSIDE the try/except so it isn't
    swallowed (st.rerun works by raising an internal exception)."""
    rep = st.session_state.rep_name.strip()
    error = None
    results = None
    try:
        with st.spinner(f"Researching {company}…"):
            results = run_pipeline(rep, company, contact, product_fit, days_back, refresh)
    except agent.ProspectingError as e:
        error = e.user_message
    except Exception as e:  # pragma: no cover - last-resort guard
        error = f"Something went wrong: {e}. Please try again."

    if error:
        st.error(error)
        return

    st.session_state.results = results
    st.session_state.feedback = {
        _fb_key(f["target_type"], f["target_id"]): f["rating"]
        for f in db.get_feedback(results["id"])
    }
    st.session_state.page = "results"
    st.rerun()


# ---------------------------------------------------------------------------
# Feedback widgets
# ---------------------------------------------------------------------------
def feedback_buttons(session_id, target_type, target_id, key_prefix, feedback_text=None):
    fb = st.session_state.feedback
    key = _fb_key(target_type, target_id)
    current = fb.get(key)

    c1, c2 = st.columns([1, 1])
    up = c1.button(
        "Helpful",
        key=f"{key_prefix}_up",
        type="primary" if current == "up" else "secondary",
        use_container_width=True,
    )
    down = c2.button(
        "Not helpful",
        key=f"{key_prefix}_down",
        type="primary" if current == "down" else "secondary",
        use_container_width=True,
    )
    if up or down:
        rating = "up" if up else "down"
        db.update_feedback(session_id, target_type, target_id, rating, feedback_text)
        fb[key] = rating
        st.rerun()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_CONF_COLOR = {"high": "green", "medium": "orange", "low": "gray"}


def render_trigger_card(session_id, t):
    label = config.TRIGGER_LABELS.get(t["trigger_type"], t["trigger_type"])
    conf = t.get("confidence", "low")
    color = _CONF_COLOR.get(conf, "gray")

    with st.container(border=True):
        st.markdown(f":blue-background[{label}] :{color}-background[{conf} confidence]")
        st.markdown(f"**{t['headline']}**")
        st.write(t["summary"])
        st.markdown(f"[Source article]({t['source_url']})")
        feedback_buttons(session_id, "trigger", t["id"], key_prefix=f"fb_trigger_{t['id']}")


def render_triggers(r):
    company = r["company"]
    days_back = r["days_back"]
    triggers = r.get("triggers", [])

    st.subheader("Triggers")
    if not triggers:
        st.info(
            f"No new triggers found in the last {days_back} days for {company}. "
            "The company snapshot above may still be useful context."
        )
        return

    top = triggers[: config.TOP_TRIGGERS_DISPLAY]
    rest = triggers[config.TOP_TRIGGERS_DISPLAY :]

    for t in top:
        render_trigger_card(r["id"], t)

    if rest:
        plural = "s" if len(rest) != 1 else ""
        with st.expander(f"Show {len(rest)} additional trigger{plural}"):
            for t in rest:
                render_trigger_card(r["id"], t)


def render_draft(r):
    draft = r.get("draft_email")
    if not draft:
        return

    st.subheader("Draft email")
    st.caption("Edit as needed, then copy into your normal email tool. Nothing is sent automatically.")
    st.text_area("Draft (editable)", value=draft, height=320, key=f"draft_text_{r['id']}")
    copy_button(draft, "Copy email")

    st.markdown("**Was this draft useful?**")
    fb_text = st.text_area(
        "Optional feedback on this draft",
        key=f"draft_fb_text_{r['id']}",
        placeholder="What worked, what you changed, what was off…",
    )
    feedback_buttons(
        r["id"], "draft", None, key_prefix=f"fb_draft_{r['id']}", feedback_text=fb_text or None
    )


def render_banners(r):
    company = r["company"]
    if r.get("from_cache"):
        c1, c2 = st.columns([4, 1])
        c1.warning(f"Showing cached results from {_fmt_ts(r['created_at'])}.")
        if c2.button("Refresh", use_container_width=True):
            do_research(
                company,
                r.get("contact"),
                r.get("product_fit") or "Other",
                r["days_back"],
                refresh=True,
            )
    elif r.get("history_days") is not None:
        n = r["history_days"]
        when = "earlier today" if n == 0 else f"{n} day{'s' if n != 1 else ''} ago"
        st.info(
            f"You researched {company} {when}. Showing only new triggers since then."
        )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def input_page():
    st.title("Deltek Prospecting Agent")
    st.write(
        "Enter a company to find recent sales trigger events, get a quick "
        "company snapshot, and draft a personalized outreach email."
    )

    with st.form("research_form"):
        company = st.text_input("Company name or domain", placeholder="e.g. Acme Defense or acme.com")
        contact = st.text_input("Contact name (optional)", placeholder="e.g. Maria Chen")
        product_fit = st.selectbox("Product fit", config.PRODUCT_OPTIONS, index=0)
        days_back = st.slider(
            "Lookback window (days)",
            config.MIN_DAYS_BACK,
            config.MAX_DAYS_BACK,
            config.DEFAULT_DAYS_BACK,
        )
        submitted = st.form_submit_button("Research", type="primary")

    if submitted:
        if not st.session_state.rep_name.strip():
            st.error("Enter your name in the sidebar before researching.")
        elif not company.strip():
            st.error("Enter a company name or domain.")
        else:
            do_research(company.strip(), contact.strip(), product_fit, days_back, refresh=False)


def results_page():
    r = st.session_state.results
    if not r:
        st.session_state.page = "input"
        st.rerun()
        return

    st.title(r["company"])
    render_banners(r)

    st.subheader("Company snapshot")
    st.write(r.get("company_snapshot") or "_No snapshot available for this company._")

    render_triggers(r)
    render_draft(r)

    st.divider()
    if st.button("← Search another account"):
        st.session_state.page = "input"
        st.session_state.results = None
        st.session_state.feedback = {}
        st.rerun()


# ---------------------------------------------------------------------------
# Sidebar (rep identity, always visible)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Rep")
    st.text_input("Your name", key="rep_name", placeholder="Required")
    st.caption(
        "Your name scopes search history and dedup so you never see the same "
        "article twice for an account."
    )

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
if st.session_state.page == "results":
    results_page()
else:
    input_page()
