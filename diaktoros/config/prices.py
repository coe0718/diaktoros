"""``prices``: what the operator says each model costs, so ``stats`` can estimate a turn's cost
where the provider reports none (#1790).

No provider the loop runs reports ``usage.cost``, so the ledger's ``cost`` is NULL on every turn,
and tokens alone mislead: a cached input token costs a fraction of an uncached one. The loop file
holds, per model as the ledger names it, US dollars per million tokens of uncached input, cached
input and output::

    "prices": {"deepseek-flash": {"input": 0.14, "cached": 0.014, "output": 0.28}}

The CLI and the form spell an entry ``MODEL=INPUT/CACHED/OUTPUT``, entries separated by commas.
{} = nothing priced. A model with no entry is unpriced and said to be, never shown as $0. The
operator maintains the table: the loop never fetches prices. Host-owned: never read from a PR.

A model on a subscription (#2241) has no per-token bill: ``{"subscription": true}``, spelled
``MODEL=subscription``. ``stats`` says ``subscription`` for it, never ``unpriced`` or $0, and
leaves it out of estimated spend. Given the model's API list prices too
(``{"subscription": true, "input": …, "cached": …, "output": …}``, spelled
``MODEL=subscription:INPUT/CACHED/OUTPUT``), ``stats`` also shows what its tokens would have
cost at API rates: an API-equivalent figure, on its own, never added to spend.
"""
from __future__ import annotations

import decimal
import math
import operator

from .. import config

PRICES_MAX = 50
MODEL_MAX = 100
PRICE_MAX = 10_000.0          # dollars per million tokens
FIELDS = ("input", "cached", "output")
SUBSCRIPTION = "subscription"
HELP = ("what a model costs, for stats' cost estimate (#1790): MODEL=INPUT/CACHED/OUTPUT in US "
        "dollars per million tokens, the model as stats names it, e.g. "
        "'deepseek-flash=0.14/0.014/0.28'; a subscription model is MODEL=subscription, or "
        "MODEL=subscription:INPUT/CACHED/OUTPUT with its API list prices for an API-equivalent "
        "figure (#2241) (repeat it; none = every model unpriced)")


def _price(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= PRICE_MAX else None


def check_prices(value, where: str) -> dict[str, dict]:
    """The loop file's ``prices``, checked: model name -> its three prices, ``{"subscription":
    true}``, or both (a subscription model with its API list prices, #2241)."""
    if value is None:
        return {}
    bad = (f"{where}: 'prices' must map model names (1-{MODEL_MAX} printable characters, no "
           f"spaces, '=' or ',') to {{\"input\", \"cached\", \"output\"}}, each 0-{PRICE_MAX:g} "
           f"US dollars per million tokens, to {{\"subscription\": true}}, or to both; at most "
           f"{PRICES_MAX} models")
    if not isinstance(value, dict) or len(value) > PRICES_MAX:
        raise config.ConfigError(bad)
    out: dict[str, dict] = {}
    for model, entry in value.items():
        name = model.strip() if isinstance(model, str) else ""
        keys = set(entry) if isinstance(entry, dict) else None
        if (not 0 < len(name) <= MODEL_MAX or not name.isprintable() or any(
                ch in name for ch in " =,") or name.lower() in out
                or keys not in (set(FIELDS), {SUBSCRIPTION}, {SUBSCRIPTION, *FIELDS})
                or SUBSCRIPTION in keys and entry[SUBSCRIPTION] is not True):
            raise config.ConfigError(bad)
        priced = {field: _price(entry[field]) for field in FIELDS if field in keys}
        if None in priced.values():
            raise config.ConfigError(bad)
        out[name.lower()] = {SUBSCRIPTION: True, **priced} if SUBSCRIPTION in keys else priced
    return out


def parse_prices(entries, where: str) -> dict[str, dict]:
    """``MODEL=INPUT/CACHED/OUTPUT``, ``MODEL=subscription`` and
    ``MODEL=subscription:INPUT/CACHED/OUTPUT`` strings (the CLI and the form's spelling) ->
    ``prices``."""
    parsed: dict[str, dict] = {}
    for text in entries:
        model, sep, rest = str(text).partition("=")
        head, colon, tail = rest.partition(":")
        subscription = head.strip().lower() == SUBSCRIPTION
        entry = _parse_numbers(tail if colon else "") if subscription else _parse_numbers(rest)
        if not sep or entry is None or not entry and (colon or not subscription):
            raise config.ConfigError(f"{where}: 'prices' entry {text!r} must be "
                                     "MODEL=INPUT/CACHED/OUTPUT (US dollars per million tokens), "
                                     "MODEL=subscription or MODEL=subscription:INPUT/CACHED/OUTPUT")
        if model.strip().lower() in parsed:
            raise config.ConfigError(f"{where}: 'prices' names {model.strip()!r} twice")
        parsed[model.strip().lower()] = {SUBSCRIPTION: True, **entry} if subscription else entry
    return check_prices(parsed, where)


def _parse_numbers(text: str) -> dict | None:
    """``INPUT/CACHED/OUTPUT`` -> its three prices; "" -> {} (none given); else None."""
    if not text:
        return {}
    numbers = text.split("/")
    try:
        return dict(zip(FIELDS, [float(n) for n in numbers])) if len(numbers) == 3 else None
    except ValueError:
        return None


def split_prices(text) -> list[str]:
    """The settings form's one line: entries separated by commas."""
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def prices_text(prices) -> str:
    """The inverse of ``split_prices`` (for ``show``): each price as the shortest text that
    reads back as the same number (#1827), never rounded. Per-token models come first, then the
    subscription ones (#2241)."""
    def entry(model: str, p: dict) -> str:
        numbers = "/".join(_number(p[field]) for field in FIELDS) if FIELDS[0] in p else ""
        if not p.get(SUBSCRIPTION):
            return f"{model}={numbers}"
        return f"{model}={SUBSCRIPTION}" + (f":{numbers}" if numbers else "")
    items = sorted((prices or {}).items(), key=lambda item: bool(item[1].get(SUBSCRIPTION)))
    return ", ".join(entry(model, p) for model, p in items)


def subscriptions(prices) -> list[str]:
    """The models the table marks as on a subscription (#2241), in table order."""
    return [model for model, p in (prices or {}).items() if p.get(SUBSCRIPTION)]


def _number(value: float) -> str:
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def prices(loop: dict) -> dict[str, dict]:
    """The model -> prices map; {} = nothing priced."""
    value = loop.get("prices")
    return dict(value) if isinstance(value, dict) else {}


def estimate(row: dict, table: dict) -> dict | None:
    """The cost of one ``stats.usage`` row (a seat and model's summed tokens) at ``table``'s
    prices, ``{"subscription": True}`` when its model is on a subscription (no per-token bill,
    #2241), or None when its model is unpriced. See ``_cost``."""
    price = table.get(str(row.get("model") or "").lower())
    if price is None:
        return None
    return {SUBSCRIPTION: True} if price.get(SUBSCRIPTION) else _cost(row, price)


def api_equivalent(row: dict, table: dict) -> dict | None:
    """What a subscription model's tokens in ``row`` would have cost at the API list prices the
    table gives for it (#2241), shaped as ``estimate``'s; None for any other row. Never spend."""
    price = table.get(str(row.get("model") or "").lower())
    return _cost(row, price) if price and price.get(SUBSCRIPTION) and FIELDS[0] in price else None


def _cost(row: dict, price: dict) -> dict:
    """``row``'s tokens at ``price``. Input the provider did not say was cached is
    priced as uncached. Where some turns did not report their cache (``cached_of`` short of
    ``tokens_in``) the estimate is then a bound, whose side the prices decide (#1826): ``at
    most`` when cached input costs less than uncached, ``at least`` when it costs more, and
    exact (``bound`` None) when they cost the same or every turn reported. ``exact`` is the
    sum in decimal with no rounding at all, each price as written, and ``dollars`` the nearest
    float on the bound's side of it, read as binary or as its JSON text, so neither the text
    nor the JSON overstates (#1832)."""
    cached = row.get("cache_read") or 0
    uncached = max((row.get("tokens_in") or 0) - cached, 0)
    with decimal.localcontext(_EXACT):
        exact = (uncached * _decimal(price["input"]) + cached * _decimal(price["cached"])
                 + (row.get("tokens_out") or 0) * _decimal(price["output"])).scaleb(-6)
    unreported = (row.get("cached_of") or 0) < (row.get("tokens_in") or 0)
    bound = (None if not unreported or price["cached"] == price["input"]
             else "at most" if price["cached"] < price["input"] else "at least")
    return {"dollars": _directed(exact, bound), "bound": bound, "exact": str(exact)}


# Precision enough for any sum of accepted prices (5e-324 to 10,000) without rounding; Inexact
# trapped, so a sum that would round raises rather than shifting a bound.
_EXACT = decimal.Context(prec=decimal.MAX_PREC, Emax=decimal.MAX_EMAX, Emin=decimal.MIN_EMIN,
                         traps=[decimal.Inexact, decimal.InvalidOperation, decimal.Overflow])


def _decimal(value: float) -> decimal.Decimal:
    return decimal.Decimal(repr(float(value)))      # the shortest text that reads back as it


def _directed(exact: decimal.Decimal, bound: str | None) -> float:
    """The float nearest ``exact``, stepped outward until it is on the bound's side both as the
    binary value and as the text JSON writes for it (its repr, which a decimal reader takes
    literally): ``0.1`` is above 0.1 in binary but not as text. Two steps at most: each moves
    the binary value a whole float, and the text lies within half a float of it."""
    near = float(exact)
    if bound is None:
        return near
    step, wrong = ((math.inf, operator.lt) if bound == "at most" else (-math.inf, operator.gt))
    while wrong(decimal.Decimal(near), exact) or wrong(decimal.Decimal(repr(near)), exact):
        near = math.nextafter(near, step)
    return near


# A bound's figure rounds away from what it bounds, so "at most" never shows less than the cost
# and "at least" never more; an exact figure rounds to nearest.
_ROUNDING = {"at most": decimal.ROUND_CEILING, "at least": decimal.ROUND_FLOOR}


def total(estimates: list[dict]) -> dict | None:
    """The sum of ``_cost`` figures (#2241: estimated spend, or an API-equivalent), shaped as
    one; None for none. Its bound is theirs when they share one (an exact figure bounds either
    way), and ``about`` when some are at most and some at least: then no side holds."""
    if not estimates:
        return None
    with decimal.localcontext(_EXACT):
        exact = sum((decimal.Decimal(e["exact"]) for e in estimates), decimal.Decimal(0))
    sides = {e["bound"] for e in estimates} - {None}
    bound = sides.pop() if len(sides) == 1 else "about" if sides else None
    return {"dollars": _directed(exact, None if bound == "about" else bound), "bound": bound,
            "exact": str(exact)}


def estimate_text(estimate_: dict | None) -> str:
    if estimate_ is None:
        return "unpriced"
    if estimate_.get(SUBSCRIPTION):
        return SUBSCRIPTION
    bound = estimate_.get("bound")
    exact = decimal.Decimal(estimate_.get("exact") or estimate_["dollars"])   # exact either way
    shown = exact.quantize(decimal.Decimal("0.0001"), context=decimal.Context(
        prec=decimal.MAX_PREC, rounding=_ROUNDING.get(bound, decimal.ROUND_HALF_EVEN)))
    return (f"{bound} " if bound else "") + f"${shown:,.4f}"
