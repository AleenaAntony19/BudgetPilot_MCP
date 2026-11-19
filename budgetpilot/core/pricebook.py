"""
Price book: per-model token rates with their source, date and validity.

Files
    budgetpilot/data/pricebook.json      bundled snapshot (shipped with the package)
    $BP_HOME/pricebook.json              refreshed copy and rates you set (`bp prices refresh` / `bp prices set`)
    $BP_HOME/pricebook.json.bak          the previous copy, kept by every write

Both are read and merged; for the same model and start date the file in
$BP_HOME wins. Each entry:

    provider, model_id, aliases, as_of (date read), valid_from (date the rate
    starts; null = since before the book), source_url, source ("bundled",
    "refresh", "user"), rates per million tokens {input, cache_write_5m,
    cache_write_1h, cache_read, output}, optional long_context
    {threshold_tokens, rates}

`price_for(provider, model, ts)` returns the rates valid at `ts` with their
provenance, or UNPRICED. It never falls back to a family guess. A model id
matches its own id, a listed alias, or "<id or alias>-YYYYMMDD": the docs say
pre-4.6 aliases resolve to such dated snapshots of the same model.

Billing units are each class's rate divided by that model's input rate, so
they weight tokens by relative price and do not depend on the price level.

Known gaps [VERIFY]: the 1.1x US-only `inference_geo` multiplier, fast-mode
prices, batch discounts and server-side tool fees are not applied; the drift
check (core/canary.py) shows when they matter.

Standard library only. Network access only in `refresh`, which the CLI calls
(never a hook).
"""

from __future__ import annotations

from collections import namedtuple
import hashlib
import json
import os
from pathlib import Path
import re
import time

RATE_KEYS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
TOKEN_FOR_RATE = {"input": "input_tokens", "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h",
                  "cache_read": "cache_read", "output": "output_tokens"}
SCHEMA = 1
STALE_DAYS = 30
MAX_JUMP = 0.50
BUNDLED = Path(__file__).resolve().parent.parent / "data" / "pricebook.json"
ANTHROPIC_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
ANTHROPIC_MODELS_URL = "https://platform.claude.com/docs/en/about-claude/models/overview"

Price = namedtuple("Price", "provider model_id rates long_context as_of valid_from source_url source")


class _Unpriced:
    """No cited rate for this model at this time. Falsy, so `if price:` works."""

    def __bool__(self):
        return False

    def __repr__(self):
        return "UNPRICED"


UNPRICED = _Unpriced()


class PriceBookError(ValueError):
    pass


# ------------------------------------------------------------------ files
def user_path() -> Path:
    from budgetpilot.core.state import get_bp_home

    return get_bp_home() / "pricebook.json"


def _read(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("entries"), list) else None
    except (OSError, ValueError):
        return None


def content_hash(entries) -> str:
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_book(entries, fetched_at: str) -> dict:
    entries = sorted(entries, key=lambda e: (e["provider"], e["model_id"], e.get("valid_from") or ""))
    return {"schema": SCHEMA, "fetched_at": fetched_at, "content_hash": content_hash(entries), "entries": entries}


def write_book(book: dict, path: Path = None) -> Path:
    """Validate, then write atomically; the previous file is kept as .bak."""
    validate_book(book)
    target = path or user_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.replace(target, target.with_name(target.name + ".bak"))
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(book, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return target


_CACHE = {}


def load(force: bool = False) -> dict:
    """Merged book: bundled entries, then $BP_HOME entries (which win on the same key)."""
    upath = user_path()
    key = (str(upath), _mtime(upath), _mtime(BUNDLED))
    if not force and _CACHE.get("key") == key:
        return _CACHE["book"]
    bundled = _read(BUNDLED) or {"entries": [], "fetched_at": None}
    user = _read(upath)
    merged = {}
    for source in (bundled, user):
        if not source:
            continue
        for e in source["entries"]:
            if _entry_ok(e):
                merged[(e["provider"], e["model_id"], e.get("valid_from"))] = e
    fetched = [d.get("fetched_at") for d in (bundled, user) if d and d.get("fetched_at")]
    book = {"fetched_at": max(fetched) if fetched else None, "entries": list(merged.values()),
            "bundled_fetched_at": bundled.get("fetched_at"), "user_file": str(upath) if user else None}
    book["entries"] += _legacy_overrides()
    _CACHE.update(key=key, book=book)
    return book


def _mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _legacy_overrides():
    """`claude_models` in $BP_HOME/prices.json (the 1.0 override format) as user entries for exact ids."""
    try:
        from budgetpilot.core.state import get_bp_home

        data = json.loads((get_bp_home() / "prices.json").read_text(encoding="utf-8"))
        rows = data.get("claude_models") or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for model, row in rows.items() if isinstance(rows, dict) else ():
        try:
            p, r = float(row["input_per_million"]), float(row.get("cache_read_multiplier", 0.1))
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"provider": "anthropic", "model_id": str(model), "aliases": [], "as_of": None,
                    "valid_from": None, "source_url": "prices.json",
                    "source": "user", "rates": {"input": p, "cache_write_5m": 1.25 * p, "cache_write_1h": 2 * p,
                                                "cache_read": r * p, "output": 5 * p}, "_override": True})
    return out


def _entry_ok(e) -> bool:
    try:
        return (isinstance(e, dict) and e.get("provider") and e.get("model_id")
                and all(float(e["rates"][k]) >= 0 for k in RATE_KEYS))
    except (KeyError, TypeError, ValueError):
        return False


# ------------------------------------------------------------------ resolution
def _matches(entry, model: str) -> bool:
    names = [entry["model_id"]] + list(entry.get("aliases") or [])
    for name in names:
        if model == name:
            return True
        if model.startswith(name + "-") and re.fullmatch(r"\d{8}", model[len(name) + 1:]):
            return True
    return False


def price_for(provider, model, ts=None, book=None):
    """Rates valid at `ts` (ISO time or epoch seconds; None = now), or UNPRICED."""
    if not model:
        return UNPRICED
    book = book or load()
    provider = provider or "anthropic"
    when = _iso(ts)
    candidates = [e for e in book["entries"] if e["provider"] == provider and _matches(e, str(model))]
    if not candidates:
        return UNPRICED
    # A user override for a model wins over everything for that model.
    overrides = [e for e in candidates if e.get("_override")]
    if overrides:
        candidates = overrides
    started = [e for e in candidates if not e.get("valid_from") or e["valid_from"] <= when]
    if not started:
        return UNPRICED  # the only known rates start after this request
    best = max(started, key=lambda e: (e.get("valid_from") or "", e.get("source") == "user"))
    return Price(best["provider"], best["model_id"], dict(best["rates"]), best.get("long_context"),
                 best.get("as_of"), best.get("valid_from"), best.get("source_url"), best.get("source"))


def _iso(ts) -> str:
    if ts is None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if isinstance(ts, (int, float)):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return str(ts)


def rates_for_request(price, tokens) -> dict:
    """The rate vector that applies to one request (long-context tier when the prompt is over the threshold)."""
    lc = price.long_context
    if lc:
        prompt = sum(int(tokens.get(k) or 0) for k in ("input_tokens", "cache_write_5m", "cache_write_1h", "cache_read"))
        if prompt > int(lc["threshold_tokens"]):
            return dict(lc["rates"])
    return price.rates


def units_and_usd(price, tokens):
    """(billing units, USD) for one request's disjoint token classes; (None, None) when unpriced."""
    if not price:
        return None, None
    rates = rates_for_request(price, tokens)
    base = float(price.rates["input"]) or 1.0
    usd = sum(float(rates[k]) * int(tokens.get(TOKEN_FOR_RATE[k]) or 0) for k in RATE_KEYS) / 1e6
    # Units use the standard tier's ratios, so a long prompt costs more dollars, not more units.
    units = sum(float(price.rates[k]) / base * int(tokens.get(TOKEN_FOR_RATE[k]) or 0) for k in RATE_KEYS)
    return units, usd


# ------------------------------------------------------------------ validation
def validate_rates(rates, label: str = "") -> None:
    try:
        v = {k: float(rates[k]) for k in RATE_KEYS}
    except (KeyError, TypeError, ValueError):
        raise PriceBookError(f"{label}: rates must have numeric {', '.join(RATE_KEYS)}") from None
    if not (0 < v["cache_read"] < v["input"] < v["cache_write_5m"] <= v["cache_write_1h"] < v["output"]):
        raise PriceBookError(f"{label}: rates out of order (expected cache_read < input < cache_write_5m "
                             f"<= cache_write_1h < output): {v}")


def validate_book(book) -> None:
    if not isinstance(book, dict) or book.get("schema") != SCHEMA or not isinstance(book.get("entries"), list):
        raise PriceBookError("not a price book (schema or entries missing)")
    if not book["entries"]:
        raise PriceBookError("price book has no entries")
    for e in book["entries"]:
        label = f"{e.get('provider')}/{e.get('model_id')}"
        if not (e.get("provider") and e.get("model_id") and e.get("source_url")):
            raise PriceBookError(f"{label}: provider, model_id and source_url are required")
        validate_rates(e.get("rates"), label)
        if e.get("long_context"):
            validate_rates(e["long_context"].get("rates"), label + " long context")
            int(e["long_context"]["threshold_tokens"])


def compare(old_book, new_entries, max_jump: float = MAX_JUMP):
    """Rate changes between the current book and new entries: (changes, jumps over max_jump, new models)."""
    changes, jumps, new_models = [], [], []
    for e in new_entries:
        old = price_for(e["provider"], e["model_id"], None, old_book)
        if not old:
            new_models.append(e["model_id"])
            continue
        for k in RATE_KEYS:
            a, b = float(old.rates[k]), float(e["rates"][k])
            if abs(a - b) > 1e-12:
                change = (e["model_id"], k, a, b)
                changes.append(change)
                if a and abs(b - a) / a > max_jump:
                    jumps.append(change)
    return changes, jumps, new_models


def age_days(book=None):
    book = book or load()
    fetched = book.get("fetched_at")
    if not fetched:
        return None
    try:
        then = time.mktime(time.strptime(fetched[:10], "%Y-%m-%d"))
    except ValueError:
        return None
    return max(0.0, (time.time() - then) / 86400)


def is_stale(book=None) -> bool:
    age = age_days(book)
    return age is None or age > STALE_DAYS


# ------------------------------------------------------------------ Anthropic parser
_MONEY = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*MTok")
_HEADERS = {"model": "Model", "input": "Base input tokens", "cache_write_5m": "5m cache writes",
            "cache_write_1h": "1h cache writes", "cache_read": "Cache hits and refreshes", "output": "Output tokens"}


def _cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _clean_name(cell: str) -> str:
    cell = re.sub(r"<sup>.*?</sup>", "", cell)
    cell = re.sub(r"\(\[[^\]]*\]\([^)]*\)\)", "", cell)          # ([limited availability](...))
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)          # [Claude X](link) -> Claude X
    return re.sub(r"\s+", " ", cell).strip()


def model_id_from_name(name: str) -> str:
    """"Claude Opus 4.1" -> "claude-opus-4-1": the pattern of every ID and model-page slug on the models page."""
    base = re.sub(r"\(.*?\)", "", name).strip().lower()
    return re.sub(r"[\s.]+", "-", base)


def parse_anthropic(pricing_md: str, models_md: str = "", as_of: str = "", source_url: str = ANTHROPIC_PRICING_URL):
    """
    Entries from the "Model pricing" table of the Markdown pricing page.
    Columns are found by header name, not position; any missing header or
    unreadable price rejects the whole page.
    """
    section = pricing_md.split("## Model pricing", 1)
    if len(section) != 2:
        raise PriceBookError("pricing page: 'Model pricing' section not found")
    lines = [ln for ln in section[1].split("\n## ", 1)[0].splitlines() if ln.strip().startswith("|")]
    if len(lines) < 3:
        raise PriceBookError("pricing page: model table not found")
    header = _cells(lines[0])
    try:
        col = {k: header.index(v) for k, v in _HEADERS.items()}
    except ValueError as exc:
        raise PriceBookError(f"pricing page: table headers changed ({header})") from exc
    aliases = _aliases_from_models_page(models_md)
    entries, long_rows = {}, {}
    for line in lines[2:]:
        cells = _cells(line)
        if len(cells) < len(header):
            continue
        name = _clean_name(cells[col["model"]])
        rates = {}
        for k in RATE_KEYS:
            m = _MONEY.search(cells[col[k]].replace("<sup>", " <sup>"))
            if not m:
                raise PriceBookError(f"pricing page: no price for {name} / {_HEADERS[k]}")
            rates[k] = float(m.group(1))
        tier = re.search(r"\(for prompts (up to|over) ([0-9,]+) tokens\)", name)
        model_id = model_id_from_name(name)
        if tier and tier.group(1) == "over":
            long_rows[model_id] = {"threshold_tokens": int(tier.group(2).replace(",", "")), "rates": rates}
            continue
        entries[model_id] = {"provider": "anthropic", "model_id": model_id, "aliases": aliases.get(model_id, []),
                             "as_of": as_of, "valid_from": None, "source_url": source_url, "source": "refresh",
                             "rates": rates, "name": re.sub(r"\(.*?\)", "", name).strip()}
    for model_id, lc in long_rows.items():
        if model_id in entries:
            entries[model_id]["long_context"] = lc
    if not entries:
        raise PriceBookError("pricing page: no model rows")
    return list(entries.values())


def _aliases_from_models_page(models_md: str) -> dict:
    """Other documented IDs (Bedrock etc.) per Claude API ID, from the models overview table."""
    out = {}
    rows = [_cells(ln) for ln in models_md.splitlines() if ln.strip().startswith("|")]
    api = next((r for r in rows if r and r[0].startswith("Claude API ID")), None)
    if not api:
        return out
    ids = [c.strip("` ") for c in api[1:]]
    for r in rows:
        if not r or r is api or "ID" not in r[0] and "alias" not in r[0]:
            continue
        for i, cell in enumerate(r[1:]):
            value = cell.strip("` ")
            if i < len(ids) and value and value != ids[i] and re.fullmatch(r"[a-z0-9.\-:]+", value):
                out.setdefault(ids[i], [])
                if value not in out[ids[i]]:
                    out[ids[i]].append(value)
    return out


# ------------------------------------------------------------------ refresh
def _fetch(url: str, timeout: float = 20.0) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "budgetpilot-pricebook"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URLs)
        return resp.read(2_000_000).decode("utf-8", errors="replace")


def fetch_anthropic(fetch=_fetch):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    pricing = fetch(ANTHROPIC_PRICING_URL + ".md")
    try:
        models = fetch(ANTHROPIC_MODELS_URL + ".md")
    except Exception:
        models = ""
    return parse_anthropic(pricing, models, as_of=today)


def fetch_unsupported(provider):
    def _stub(fetch=_fetch):
        raise PriceBookError(f"{provider}: automatic refresh is not implemented [VERIFY the official pricing page "
                             f"format]; set rates with `bp prices set {provider} MODEL ...`")
    return _stub


PROVIDERS = {"anthropic": fetch_anthropic, "openai": fetch_unsupported("openai"), "google": fetch_unsupported("google")}


def refresh(providers=("anthropic",), confirm: bool = False, fetch=_fetch, path: Path = None) -> dict:
    """
    Fetch, parse and validate; write only if everything passes. Changed rates
    are added as new entries valid from today, so older usage keeps its old
    price. Any rate moving more than 50% needs `confirm`. A network or parse
    failure leaves the current file untouched.
    """
    target = path or user_path()
    current_user = _read(target) or {"entries": []}
    book = load(force=True)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report = {"ok": False, "providers": {}, "changes": [], "jumps": [], "new_models": [], "written": None}
    added = []
    for provider in providers:
        try:
            entries = PROVIDERS[provider](fetch=fetch)
        except KeyError:
            report["providers"][provider] = "unknown provider"
            continue
        except PriceBookError as exc:
            report["providers"][provider] = str(exc)
            continue
        except Exception as exc:  # network: offline-first, keep the current file
            report["providers"][provider] = f"fetch failed ({type(exc).__name__}); kept the current price book"
            continue
        changes, jumps, new_models = compare(book, entries)
        report["changes"] += changes
        report["jumps"] += jumps
        report["new_models"] += new_models
        changed_ids = {c[0] for c in changes}
        for e in entries:
            if e["model_id"] in changed_ids:
                e = {**e, "valid_from": today + "T00:00:00Z"}
            elif e["model_id"] not in new_models:
                e = {**e, "valid_from": _current_valid_from(book, e)}
            added.append(e)
        report["providers"][provider] = f"{len(entries)} models read"
    if report["jumps"] and not confirm:
        report["error"] = ("rejected: some rates moved more than 50%; check the source and re-run with --confirm: "
                           + ", ".join(f"{m} {k} {a}->{b}" for m, k, a, b in report["jumps"]))
        return report
    if not added:
        report["error"] = "nothing fetched; the current price book is unchanged"
        return report
    keep = [e for e in current_user["entries"]
            if (e["provider"], e["model_id"], e.get("valid_from")) not in
            {(a["provider"], a["model_id"], a.get("valid_from")) for a in added}]
    new_book = make_book(keep + added, now)
    try:
        report["written"] = str(write_book(new_book, target))
    except PriceBookError as exc:
        report["error"] = f"rejected: {exc}"
        return report
    load(force=True)
    report["ok"] = True
    return report


def _current_valid_from(book, entry):
    price = price_for(entry["provider"], entry["model_id"], None, book)
    return price.valid_from if price else None


def set_rates(provider: str, model: str, rates: dict, source_url: str = "set by user", path: Path = None) -> dict:
    """Add or replace a user-set entry (valid from now on); validated like any other."""
    validate_rates(rates, f"{provider}/{model}")
    target = path or user_path()
    current = _read(target) or {"entries": []}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    entry = {"provider": provider, "model_id": model, "aliases": [], "as_of": today,
             "valid_from": today + "T00:00:00Z", "source_url": source_url, "source": "user",
             "rates": {k: float(rates[k]) for k in RATE_KEYS}}
    keep = [e for e in current["entries"] if not (e["provider"] == provider and e["model_id"] == model
                                                  and e.get("valid_from") == entry["valid_from"])]
    # Setting one rate is not a refresh: keep the book's fetch date so staleness stays honest.
    fetched = current.get("fetched_at") or load().get("bundled_fetched_at") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_book(make_book(keep + [entry], fetched), target)
    load(force=True)
    return entry
