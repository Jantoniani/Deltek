"""Core prospecting logic — UI-agnostic.

Every function here is callable from a notebook or a script; nothing imports
Streamlit. The pipeline is deterministic and fixed in order:

    search_news -> classify_triggers -> rank_triggers -> research_company -> draft_email

API keys are read from the environment (see config.get_secret). The Streamlit
app bridges st.secrets into os.environ before calling any of this.

Shared data shapes
------------------
article  = {"index": int, "title": str, "url": str,
            "published_date": str | None, "text": str}
trigger  = {"trigger_type": str, "headline": str, "summary": str,
            "source_url": str, "confidence": "high"|"medium"|"low",
            "published_date": str | None, "rank": int}
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Errors  (each carries a .user_message safe to show a rep)
# ---------------------------------------------------------------------------
class ProspectingError(Exception):
    def __init__(self, user_message: str, *, original: Exception | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.original = original


class ConfigError(ProspectingError):
    pass


class ExaError(ProspectingError):
    pass


class ExaRateLimitError(ExaError):
    pass


class ClaudeError(ProspectingError):
    pass


class ClaudeRateLimitError(ClaudeError):
    pass


class CompanyNotFoundError(ProspectingError):
    pass


# ---------------------------------------------------------------------------
# Lazy clients  (created on first use so importing this module is cheap and
# never requires keys to be present, e.g. when imported for unit tests)
# ---------------------------------------------------------------------------
_exa_client = None
_anthropic_client = None


def _exa():
    global _exa_client
    if _exa_client is None:
        try:
            from exa_py import Exa
        except ImportError as e:  # pragma: no cover
            raise ConfigError(
                "The 'exa_py' package is not installed. Run: pip install -r requirements.txt",
                original=e,
            )
        key = config.get_secret("EXA_API_KEY")
        if not key:
            raise ConfigError(
                "EXA_API_KEY is not configured. Add it to .streamlit/secrets.toml "
                "(or your .env file) and restart."
            )
        _exa_client = Exa(key)
    return _exa_client


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise ConfigError(
                "The 'anthropic' package is not installed. Run: pip install -r requirements.txt",
                original=e,
            )
        key = config.get_secret("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not configured. Add it to .streamlit/secrets.toml "
                "(or your .env file) and restart."
            )
        _anthropic_client = anthropic.Anthropic(api_key=key)
    return _anthropic_client


def _call_claude(model: str, max_tokens: int, prompt: str, tools=None, tool_name=None):
    """Single Claude call with consistent error translation.

    Returns the raw message object. Callers extract text or tool input.
    """
    import anthropic

    client = _anthropic()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = {"type": "tool", "name": tool_name}

    try:
        return client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        raise ClaudeRateLimitError(
            "Claude API rate limit reached. Wait 60 seconds and try again.",
            original=e,
        )
    except anthropic.APIStatusError as e:
        msg = getattr(e, "message", None) or str(e)
        raise ClaudeError(
            f"Claude API returned an error: {msg}. Try again, or check status at "
            "https://status.anthropic.com.",
            original=e,
        )
    except anthropic.APIConnectionError as e:
        raise ClaudeError(
            "Could not reach the Claude API (network error). Check your connection and try again.",
            original=e,
        )
    except Exception as e:  # pragma: no cover - defensive
        raise ClaudeError(
            f"Unexpected error calling Claude: {e}. Try again, or check status at "
            "https://status.anthropic.com.",
            original=e,
        )


def _claude_text(message) -> str:
    """Concatenate all text blocks from a Claude message."""
    parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------
_prompt_cache: dict[str, str] = {}


def load_prompt(name: str) -> str:
    if name not in _prompt_cache:
        _prompt_cache[name] = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    return _prompt_cache[name]


def fill(template: str, **kwargs) -> str:
    """Fill {placeholder} tokens via literal replacement.

    Uses str.replace (not str.format) so literal JSON braces in the prompt
    files are left untouched.
    """
    out = template
    for key, value in kwargs.items():
        out = out.replace("{" + key + "}", str(value))
    return out


# ---------------------------------------------------------------------------
# Exa helpers
# ---------------------------------------------------------------------------
def _exa_search(query: str, *, num_results: int, days_back: int | None = None,
                category: str | None = None):
    """Run one Exa search_and_contents call with excerpt text, translating errors."""
    exa = _exa()
    kwargs = {
        "query": query,
        "type": "auto",
        "num_results": num_results,
        "text": {"max_characters": config.EXCERPT_MAX_CHARS},
    }
    if category:
        kwargs["category"] = category
    if days_back is not None:
        start = datetime.now(timezone.utc) - timedelta(days=days_back)
        kwargs["start_published_date"] = start.strftime("%Y-%m-%dT00:00:00.000Z")

    try:
        result = exa.search_and_contents(**kwargs)
        return result.results or []
    except Exception as e:
        _raise_exa_error(e)


def _raise_exa_error(e: Exception):
    """Translate an Exa/requests exception into a friendly ProspectingError."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    text = str(e).lower()
    if status == 429 or "rate limit" in text or "too many requests" in text:
        raise ExaRateLimitError(
            "Exa API rate limit reached. Wait 60 seconds and try again.", original=e
        )
    if status in (401, 403) or "unauthorized" in text or "forbidden" in text:
        raise ExaError(
            "Exa API rejected the request (check that EXA_API_KEY is valid).", original=e
        )
    raise ExaError(f"Exa search failed: {e}. Try again in a moment.", original=e)


def fetch_article_content(url: str) -> str:
    """Fetch the full text of one URL for the drafting call. Non-fatal: returns
    "" if it fails, since the draft can still proceed from the trigger summary."""
    try:
        exa = _exa()
        result = exa.get_contents(
            [url], text={"max_characters": config.FULL_CONTENT_MAX_CHARS}
        )
        results = result.results or []
        if results and getattr(results[0], "text", None):
            return results[0].text.strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Action 1: search_news
# ---------------------------------------------------------------------------
def search_news(company: str, days_back: int = config.DEFAULT_DAYS_BACK,
                exclude_urls: set[str] | list[str] | None = None) -> list[dict]:
    """Find recent candidate news articles about `company`, excluding URLs the
    rep has already seen. Returns a list of article dicts (see module docstring).

    Dedup is URL-level: any URL in `exclude_urls` is filtered out before return.
    """
    exclude = {u for u in (exclude_urls or [])}
    raw = _exa_search(
        f"Recent news about {company}",
        num_results=config.NEWS_RESULTS,
        days_back=days_back,
        category="news",
    )
    time.sleep(config.EXA_RATE_LIMIT_SLEEP)

    articles: list[dict] = []
    idx = 0
    for r in raw:
        url = getattr(r, "url", None)
        if not url or url in exclude:
            continue
        articles.append(
            {
                "index": idx,
                "title": getattr(r, "title", None) or "(untitled)",
                "url": url,
                "published_date": getattr(r, "published_date", None),
                "text": (getattr(r, "text", None) or "").strip(),
            }
        )
        idx += 1
    return articles


# ---------------------------------------------------------------------------
# Action 2: classify_triggers
# ---------------------------------------------------------------------------
def _classify_tool(taxonomy: dict) -> dict:
    return {
        "name": "record_triggers",
        "description": "Record the sales trigger events found in the candidate articles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "triggers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "article_index": {
                                "type": "integer",
                                "description": "The index of the source article.",
                            },
                            "trigger_type": {
                                "type": "string",
                                "enum": list(taxonomy.keys()),
                            },
                            "headline": {"type": "string"},
                            "summary": {"type": "string"},
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                        },
                        "required": [
                            "article_index",
                            "trigger_type",
                            "headline",
                            "summary",
                            "confidence",
                        ],
                    },
                }
            },
            "required": ["triggers"],
        },
    }


def _format_articles(articles: list[dict]) -> str:
    blocks = []
    for a in articles:
        date = a.get("published_date") or "unknown date"
        excerpt = (a.get("text") or "").strip()
        blocks.append(
            f"[{a['index']}] {a['title']}\n"
            f"    published: {date}\n"
            f"    url: {a['url']}\n"
            f"    excerpt: {excerpt}"
        )
    return "\n\n".join(blocks)


def _format_taxonomy(taxonomy: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in taxonomy.items())


def classify_triggers(articles: list[dict], taxonomy: dict | None = None,
                      company: str = "") -> list[dict]:
    """Classify candidate articles into triggers via a single Haiku tool-use call.

    Articles that aren't real triggers are simply omitted by the model. Returns
    unranked trigger dicts enriched with source_url and published_date from the
    originating article. Call `rank_triggers` to order them.
    """
    if not articles:
        return []
    taxonomy = taxonomy or config.TRIGGER_TAXONOMY

    prompt = fill(
        load_prompt("classify.txt"),
        company=company or "the company",
        taxonomy=_format_taxonomy(taxonomy),
        articles=_format_articles(articles),
    )
    tool = _classify_tool(taxonomy)
    message = _call_claude(
        config.CLASSIFY_MODEL,
        config.CLASSIFY_MAX_TOKENS,
        prompt,
        tools=[tool],
        tool_name="record_triggers",
    )

    raw_triggers = None
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_triggers":
            raw_triggers = block.input.get("triggers", [])
            break
    if raw_triggers is None:
        # Model didn't call the tool (rare with forced tool_choice). Treat as
        # "no triggers" rather than erroring — the snapshot is still useful.
        return []

    by_index = {a["index"]: a for a in articles}
    triggers: list[dict] = []
    for t in raw_triggers:
        try:
            idx = int(t["article_index"])
        except (KeyError, TypeError, ValueError):
            continue
        article = by_index.get(idx)
        if not article:
            continue
        ttype = t.get("trigger_type")
        if ttype not in taxonomy:
            ttype = "other_significant_event"
        confidence = t.get("confidence", "low")
        if confidence not in _CONFIDENCE_ORDER:
            confidence = "low"
        triggers.append(
            {
                "trigger_type": ttype,
                "headline": (t.get("headline") or article["title"]).strip(),
                "summary": (t.get("summary") or "").strip(),
                "source_url": article["url"],
                "confidence": confidence,
                "published_date": article.get("published_date"),
            }
        )
    return triggers


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def _recency_key(published_date) -> float:
    """Higher = more recent. Missing/unparseable dates sort last."""
    if not published_date:
        return float("-inf")
    raw = str(published_date).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        try:
            return datetime.strptime(str(published_date)[:10], "%Y-%m-%d").timestamp()
        except ValueError:
            return float("-inf")


def rank_triggers(triggers: list[dict]) -> list[dict]:
    """Rank by confidence (high > medium > low), then recency. Assigns `rank`
    starting at 1 and returns a new sorted list."""
    ordered = sorted(
        triggers,
        key=lambda t: (
            _CONFIDENCE_ORDER.get(t.get("confidence", "low"), 2),
            -_recency_key(t.get("published_date")),
        ),
    )
    for i, t in enumerate(ordered):
        t["rank"] = i + 1
    return ordered


# ---------------------------------------------------------------------------
# Action 3: research_company
# ---------------------------------------------------------------------------
def research_company(company: str) -> str:
    """Return a 2-4 sentence company snapshot synthesized from Exa results.

    Returns "" if Exa surfaces no source material for the company (the caller
    decides whether that, combined with zero news, means "company not found").
    """
    results = _exa_search(
        f"{company} company overview profile what they do industry size headquarters",
        num_results=config.SNAPSHOT_RESULTS,
    )
    time.sleep(config.EXA_RATE_LIMIT_SLEEP)
    if not results:
        return ""

    blocks = []
    for r in results:
        title = getattr(r, "title", None) or "(untitled)"
        excerpt = (getattr(r, "text", None) or "").strip()
        if excerpt:
            blocks.append(f"- {title}: {excerpt}")
    if not blocks:
        return ""

    prompt = fill(
        load_prompt("snapshot.txt"),
        company=company,
        search_results="\n".join(blocks),
    )
    message = _call_claude(config.SNAPSHOT_MODEL, config.SNAPSHOT_MAX_TOKENS, prompt)
    return _claude_text(message)


# ---------------------------------------------------------------------------
# Action 4: draft_email
# ---------------------------------------------------------------------------
def draft_email(trigger: dict, snapshot: str, product_fit: str,
                rep_name: str, contact_name: str | None,
                company: str | None = None) -> str:
    """Draft a complete outbound email (subject + body) anchored on `trigger`.

    Uses Sonnet for writing quality. Fetches the trigger's source article in
    full to give the model concrete detail; the fetch is best-effort. `company`
    defaults to a "company" field on the trigger dict if not passed explicitly.
    """
    positioning = config.PRODUCT_POSITIONING.get(
        product_fit, config.PRODUCT_POSITIONING["Other"]
    )
    company = (company or trigger.get("company") or "the company").strip()

    trigger_lines = [
        f"Type: {config.TRIGGER_LABELS.get(trigger['trigger_type'], trigger['trigger_type'])}",
        f"Headline: {trigger['headline']}",
        f"Summary: {trigger['summary']}",
    ]
    full = fetch_article_content(trigger.get("source_url", ""))
    if full:
        trigger_lines.append(f"Article detail: {full[:1500]}")
    trigger_block = "\n".join(trigger_lines)

    contact = (contact_name or "").strip() or "there"

    prompt = fill(
        load_prompt("draft_email.txt"),
        rep_name=rep_name.strip() or "Your Name",
        contact_name=contact,
        company=company,
        trigger=trigger_block,
        snapshot=snapshot or "(no snapshot available)",
        product=positioning,
    )
    message = _call_claude(config.DRAFT_MODEL, config.DRAFT_MAX_TOKENS, prompt)
    return _claude_text(message)
