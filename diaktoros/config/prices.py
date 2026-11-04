"""``prices``: what the operator says each model costs, so ``stats`` can estimate a turn's cost
where the provider reports none (#1790).

No provider the loop runs reports ``usage.cost``, so the ledger's ``cost`` is NULL on every turn,
and tokens alone mislead: a cached input token costs a fraction of an uncached one. The loop file
holds, per model as the ledger names it, US dollars per million tokens of uncached input, cached
input and output::

    "prices": {"deepseek-flash": {"input": 0.14, "cached": 0.014, "output": 0.28}}

The CLI and the form spell an entry ``MODEL=INPUT/CACHED/OUTPUT``, entries separated by commas.
{} = nothing priced. A model with no entry is unpriced and said to be, never shown as $0, and a
subscription model has no per-token price to enter. The operator maintains the table: the loop
never fetches prices. Host-owned: never read from a PR.
"""
from __future__ import annotations

import decimal
import math

from .. import config

PRICES_MAX = 50
MODEL_MAX = 100
PRICE_MAX = 10_000.0          # dollars per million tokens
FIELDS = ("input", "cached", "output")
HELP = ("what a model costs, for stats' cost estimate (#1790): MODEL=INPUT/CACHED/OUTPUT in US "
        "dollars per million tokens, the model as stats names it, e.g. "
        "'deepseek-flash=0.14/0.014/0.28' (repeat it; none = every model unpriced)")


def _price(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= PRICE_MAX else None


def check_prices(value, where: str) -> dict[str, dict[str, float]]:
    """The loop file's ``prices``, checked: model name -> its three prices."""
    if value is None:
        return {}
    bad = (f"{where}: 'prices' must map model names (1-{MODEL_MAX} printable characters, no "
           f"spaces, '=' or ',') to {{\"input\", \"cached\", \"output\"}}, each 0-{PRICE_MAX:g} "
           f"US dollars per million tokens; at most {PRICES_MAX} models")
    if not isinstance(value, dict) or len(value) > PRICES_MAX:
        raise config.ConfigError(bad)
    out: dict[str, dict[str, float]] = {}
    for model, entry in value.items():
        name = model.strip() if isinstance(model, str) else ""
        if (not 0 < len(name) <= MODEL_MAX or not name.isprintable() or any(
                ch in name for ch in " =,") or name.lower() in out
                or not isinstance(entry, dict) or set(entry) != set(FIELDS)):
            raise config.ConfigError(bad)
        priced = {field: _price(entry[field]) for field in FIELDS}
        if None in priced.values():
            raise config.ConfigError(bad)
        out[name.lower()] = priced
    return out


def parse_prices(entries, where: str) -> dict[str, dict[str, float]]:
    """``MODEL=INPUT/CACHED/OUTPUT`` strings (the CLI and the form's spelling) -> ``prices``."""
    parsed: dict[str, dict] = {}
    for text in entries:
        model, sep, rest = str(text).partition("=")
        numbers = rest.split("/")
        try:
            values = [float(n) for n in numbers] if sep and len(numbers) == 3 else None
        except ValueError:
            values = None
        if values is None:
            raise config.ConfigError(f"{where}: 'prices' entry {text!r} must be "
                                     "MODEL=INPUT/CACHED/OUTPUT (US dollars per million tokens)")
        if model.strip().lower() in parsed:
            raise config.ConfigError(f"{where}: 'prices' names {model.strip()!r} twice")
        parsed[model.strip().lower()] = dict(zip(FIELDS, values))
    return check_prices(parsed, where)


def split_prices(text) -> list[str]:
    """The settings form's one line: entries separated by commas."""
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def prices_text(prices) -> str:
    """The inverse of ``split_prices`` (for ``show``): each price as the shortest text that
    reads back as the same number (#1827), never rounded."""
    return ", ".join(f"{model}=" + "/".join(_number(p[field]) for field in FIELDS)
                     for model, p in (prices or {}).items())


def _number(value: float) -> str:
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


def prices(loop: dict) -> dict[str, dict[str, float]]:
    """The model -> prices map; {} = nothing priced."""
    value = loop.get("prices")
    return dict(value) if isinstance(value, dict) else {}


def estimate(row: dict, table: dict) -> dict | None:
    """The cost of one ``stats.usage`` row (a seat and model's summed tokens) at ``table``'s
    prices, or None when its model is unpriced. Input the provider did not say was cached is
    priced as uncached. Where some turns did not report their cache (``cached_of`` short of
    ``tokens_in``) the estimate is then a bound, whose side the prices decide (#1826): ``at
    most`` when cached input costs less than uncached, ``at least`` when it costs more, and
    exact (``bound`` None) when they cost the same or every turn reported. ``exact`` is the
    sum in decimal, each price as written, so the text can round a bound outward (#1832)."""
    price = table.get(str(row.get("model") or "").lower())
    if price is None:
        return None
    cached = row.get("cache_read") or 0
    uncached = max((row.get("tokens_in") or 0) - cached, 0)
    with decimal.localcontext(prec=80):
        exact = (uncached * _decimal(price["input"]) + cached * _decimal(price["cached"])
                 + (row.get("tokens_out") or 0) * _decimal(price["output"])) / 10 ** 6
    unreported = (row.get("cached_of") or 0) < (row.get("tokens_in") or 0)
    bound = (None if not unreported or price["cached"] == price["input"]
             else "at most" if price["cached"] < price["input"] else "at least")
    return {"dollars": float(exact), "bound": bound, "exact": str(exact)}


def _decimal(value: float) -> decimal.Decimal:
    return decimal.Decimal(repr(float(value)))      # the shortest text that reads back as it


# A bound's figure rounds away from what it bounds, so "at most" never shows less than the cost
# and "at least" never more; an exact figure rounds to nearest.
_ROUNDING = {"at most": decimal.ROUND_CEILING, "at least": decimal.ROUND_FLOOR}


def estimate_text(estimate_: dict | None) -> str:
    if estimate_ is None:
        return "unpriced"
    bound = estimate_.get("bound")
    exact = decimal.Decimal(estimate_.get("exact") or repr(float(estimate_["dollars"])))
    shown = exact.quantize(decimal.Decimal("0.0001"),
                           rounding=_ROUNDING.get(bound, decimal.ROUND_HALF_EVEN))
    return (f"{bound} " if bound else "") + f"${shown:,.4f}"
