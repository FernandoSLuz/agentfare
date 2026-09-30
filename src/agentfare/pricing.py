"""Price table: ``pricing.yaml`` -> :class:`PriceTable`.

Prices are USD per 1M tokens and are held as ``Decimal`` (never float). Any field
that could not be verified against the provider's official page is the literal
string ``unverified``; a model whose input or output price is unverified is
*unpriced*: it is excluded from dollar totals and the CLI warns about it.

File format (``src/agentfare/data/pricing.yaml``)::

    schema: 1
    models:
      gpt-6.1-sol:
        provider: openai
        input: 2.0
        cached_input: 0.10
        output: 10.0
        cache_write: 2.5        # optional, 5-minute cache writes, defaults to the input price
        cache_write_1h: 4.0     # optional, 1-hour cache writes, defaults to the cache_write price
        source: https://...     # official pricing page
        checked: 2026-09-30     # date the price was verified
        note: free text         # optional
    aliases:
      some-dated-id: canonical-id
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .safeio import MAX_CONFIG_BYTES, read_text_limited
from .safeyaml import UniqueKeyLoader, YamlBombError, check_expansion, shown as _shown

UNVERIFIED = "unverified"
PRICE_FIELDS = ("input", "cached_input", "output", "cache_write", "cache_write_1h")


class PricingError(ValueError):
    pass


# Sanity limits: a real table has a few dozen models with short ids and prices like 2.5 or 0.0625. A value outside these is a
# typo or an attack (``1e100000`` makes a Decimal whose formatting is a hundred thousand digits long).
MAX_PRICE = Decimal(1_000_000)  # USD per 1M tokens
MAX_PRICE_DIGITS = 100  # the cost code keeps every digit of a price (tests use 40); a hundred is already beyond any real one
MIN_PRICE_EXPONENT = -100
MAX_NUMBER_TEXT = 128
MAX_NAME_CHARS = 200  # model ids and aliases
TEXT_LIMITS = {"provider": 100, "source": 2000, "checked": 40, "note": 5000}


@dataclass(frozen=True)
class ModelPrice:
    model: str
    provider: str = ""
    input: Decimal | None = None
    cached_input: Decimal | None = None
    output: Decimal | None = None
    cache_write: Decimal | None = None
    cache_write_1h: Decimal | None = None
    source: str = ""
    checked: str = ""
    note: str = ""
    unverified_fields: tuple[str, ...] = ()

    @property
    def priced(self) -> bool:
        """True when dollar amounts can be computed (input and output are verified numbers)."""
        return self.input is not None and self.output is not None

    @property
    def cached_rate(self) -> Decimal | None:
        return self.cached_input if self.cached_input is not None else self.input

    @property
    def cache_write_rate(self) -> Decimal | None:
        return self.cache_write if self.cache_write is not None else self.input

    @property
    def cache_write_1h_rate(self) -> Decimal | None:
        return self.cache_write_1h if self.cache_write_1h is not None else self.cache_write_rate


@dataclass
class PriceTable:
    models: dict[str, ModelPrice] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    # ---- lookup --------------------------------------------------------------------
    def resolve(self, model: str) -> str | None:
        """Canonical model id for a model string seen in a log, or None if unknown."""
        for cand in _candidates(model):
            if cand in self.models:
                return cand
            if cand in self.aliases and self.aliases[cand] in self.models:
                return self.aliases[cand]
        return None

    def get(self, model: str) -> ModelPrice | None:
        canonical = self.resolve(model)
        return self.models[canonical] if canonical else None

    def priced(self, model: str) -> ModelPrice | None:
        """Price entry only when dollar math is possible for it."""
        p = self.get(model)
        return p if p and p.priced else None

    # ---- reporting -----------------------------------------------------------------
    def status(self, model: str) -> str:
        """'priced' | 'unverified' | 'unknown'."""
        p = self.get(model)
        if p is None:
            return "unknown"
        return "priced" if p.priced else UNVERIFIED

    def warning_for(self, model: str) -> str | None:
        p = self.get(model)
        if p is None:
            return f"{model}: not in the price table, its spend is not counted (add it to pricing.yaml)"
        if not p.priced:
            return f"{model}: price is unverified, its spend is not counted (see pricing.yaml)"
        return _fallback_warning(model, p)

    def unverified_models(self) -> list[str]:
        return sorted(m for m, p in self.models.items() if not p.priced or p.unverified_fields)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _fallback_warning(model: str, p: ModelPrice) -> str | None:
    """Warning for a priced model whose optional prices are unverified: those tokens are billed at a fallback rate."""
    cached, written, hour = (f in p.unverified_fields for f in ("cached_input", "cache_write", "cache_write_1h"))
    names = [name for name, flag in (("cached-input", cached), ("cache-write", written), ("1-hour cache-write", hour)) if flag]
    if not names:
        return None
    at_input = [what for what, flag in (("reads", cached), ("writes", written)) if flag]
    billed = [f"cache {_join(at_input)} are billed at the input rate"] if at_input else []
    if hour:
        billed.append(f"1-hour cache writes are billed at the {'input' if p.cache_write is None else '5-minute cache-write'} rate")
    return f"{model}: {_join(names)} {'prices are' if len(names) > 1 else 'price is'} unverified, {'; '.join(billed)}"


# ---- normalisation --------------------------------------------------------------------
_DATE_SUFFIX = re.compile(r"-(?:20\d{6}|20\d{2}-\d{2}-\d{2})$")
_CTX_SUFFIX = re.compile(r"\[[^\]]*\]$")


def _candidates(model: str) -> list[str]:
    """Progressively normalised spellings of a model id, most specific first."""
    m = model.strip().lower()
    out = [m]
    for prefix in ("anthropic/", "openai/", "google/", "xai/", "models/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
            out.append(m)
    m2 = _CTX_SUFFIX.sub("", m)
    if m2 != m:
        m = m2
        out.append(m)
    if m.endswith("-latest"):
        m = m[: -len("-latest")]
        out.append(m)
    m3 = _DATE_SUFFIX.sub("", m)
    if m3 != m:
        out.append(m3)
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


# ---- parsing --------------------------------------------------------------------------
def _num(value: Any, where: str) -> tuple[Decimal | None, bool]:
    """(number, is_unverified). ``None``/missing -> (None, False)."""
    if value is None:
        return None, False
    # A plain scalar first: str() or Decimal() of a list built by YAML aliases would never finish.
    if isinstance(value, str):
        if len(value) > MAX_NUMBER_TEXT:
            raise PricingError(f"{where}: expected a number or 'unverified', got text of {len(value):,} characters")
        if value.strip().lower() == UNVERIFIED:
            return None, True
    elif isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise PricingError(f"{where}: expected a number or 'unverified', got {_shown(value)}")
    elif isinstance(value, int) and value.bit_length() > 100:  # far above MAX_PRICE, and str() of a huge int is refused by Python
        raise PricingError(f"{where}: {_shown(value)} is more than {MAX_PRICE:,} USD per 1M tokens")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise PricingError(f"{where}: expected a number or 'unverified', got {_shown(value)}") from exc
    if not d.is_finite():
        raise PricingError(f"{where}: expected a number or 'unverified', got {_shown(value)}")
    if d < 0:
        raise PricingError(f"{where}: negative price")
    if d > MAX_PRICE:
        raise PricingError(f"{where}: {_shown(value)} is more than {MAX_PRICE:,} USD per 1M tokens")
    _, digits, exponent = d.as_tuple()
    if len(digits) > MAX_PRICE_DIGITS or (d and exponent < MIN_PRICE_EXPONENT):  # type: ignore[operator]
        raise PricingError(f"{where}: {_shown(value)} has more than {MAX_PRICE_DIGITS} digits or {-MIN_PRICE_EXPONENT} decimals")
    return d, False


def _text(value: Any, where: str, limit: int) -> str:
    """A text field: a scalar (text, a number, a date) of at most ``limit`` characters."""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, Decimal, bool, date)):
        raise PricingError(f"{where}: expected text, got {_shown(value)}")
    if isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 200:
        raise PricingError(f"{where}: expected text, got {_shown(value)}")
    text = str(value)
    if len(text) > limit:
        raise PricingError(f"{where}: longer than {limit:,} characters")
    return text


def parse_pricing(doc: dict[str, Any], path: Path | None = None) -> PriceTable:
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
        raise PricingError("pricing file needs a top-level 'models' mapping")
    models: dict[str, ModelPrice] = {}
    for name, entry in doc["models"].items():
        key = _text(name, "model id", MAX_NAME_CHARS).strip().lower()
        if not isinstance(entry, dict):
            raise PricingError(f"{name}: expected a mapping")
        if key in models:
            raise PricingError(f"{name}: duplicate model id (ids are case-insensitive, {key!r} is already defined)")
        vals: dict[str, Decimal | None] = {}
        unverified: list[str] = []
        for f in PRICE_FIELDS:
            v, unv = _num(entry.get(f), f"{name}.{f}")
            vals[f] = v
            if unv:
                unverified.append(f)
        models[key] = ModelPrice(
            model=key,
            provider=_text(entry.get("provider"), f"{name}.provider", TEXT_LIMITS["provider"]),
            source=_text(entry.get("source"), f"{name}.source", TEXT_LIMITS["source"]),
            checked=_text(entry.get("checked"), f"{name}.checked", TEXT_LIMITS["checked"]),
            note=_text(entry.get("note"), f"{name}.note", TEXT_LIMITS["note"]),
            unverified_fields=tuple(unverified),
            **vals,
        )
    raw_aliases = doc.get("aliases") or {}
    if not isinstance(raw_aliases, dict):
        raise PricingError("'aliases' must be a mapping of alias -> model id")
    aliases: dict[str, str] = {}
    for alias, target in raw_aliases.items():
        name = _text(alias, "alias", MAX_NAME_CHARS).strip().lower()
        if name in aliases:
            raise PricingError(f"{_shown(alias)}: duplicate alias (aliases are case-insensitive, {name!r} is already defined)")
        if not isinstance(target, (str, int, float, Decimal, bool, date)):
            raise PricingError(f"alias {name}: expected a model id, got {_shown(target)}")
        aliases[name] = _text(target, f"alias {name}", MAX_NAME_CHARS).strip().lower()
    return PriceTable(models=models, aliases=aliases, path=path)


def builtin_path() -> Path:
    return Path(str(resources.files("agentfare").joinpath("data/pricing.yaml")))


class _ExactLoader(UniqueKeyLoader):
    """Reads YAML floats as ``Decimal`` straight from their text: ``0.10`` stays ``0.10``, with no binary float in between.
    A key written twice in a mapping is an error (``UniqueKeyLoader``)."""


def _exact_float(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Any:
    try:
        return Decimal(str(loader.construct_scalar(node)).replace("_", ""))
    except InvalidOperation:  # .inf, .nan, base-60 floats: let PyYAML decide (they are rejected as prices later)
        return yaml.SafeLoader.construct_yaml_float(loader, node)


_ExactLoader.add_constructor("tag:yaml.org,2002:float", _exact_float)


def load_pricing(cfg: Config | None = None, path: Path | None = None) -> PriceTable:
    """Explicit ``path``, else the configured ``pricing_file`` (an error when it is missing), else the optional
    ``~/.agentfare/pricing.yaml``, else the built-in table."""
    if path is None and cfg is not None:
        if cfg.pricing_override_path.exists():
            path = cfg.pricing_override_path
        elif cfg.data.get("pricing_file"):  # only the implicit file is optional: a path the user wrote must be there
            raise PricingError(f"pricing_file {cfg.pricing_override_path} does not exist")
    target = path or builtin_path()
    try:
        text = read_text_limited(target, MAX_CONFIG_BYTES)  # a regular file of at most 10 MB, never /dev/zero or a pipe
    except (OSError, UnicodeDecodeError) as exc:
        raise PricingError(f"cannot read {target}: {exc}") from exc
    try:
        doc = yaml.load(text, Loader=_ExactLoader)
    except Exception as exc:  # not only YAMLError: PyYAML lets ValueError, KeyError and more escape for values like 2026-02-30
        raise PricingError(f"cannot read {target}: {exc}") from exc
    try:
        check_expansion(doc)  # aliases that expand to billions of values load instantly and blow up at the first str()
    except YamlBombError as exc:
        raise PricingError(f"cannot read {target}: {exc}") from exc
    return parse_pricing(doc, target)


# ---- writing --------------------------------------------------------------------------
def _fmt(d: Decimal | None, unverified: bool) -> str | None:
    if unverified:
        return UNVERIFIED
    if d is None:
        return None
    s = format(d, "f")
    return s if "." in s else s + ".0"


# Characters PyYAML refuses to read anywhere in a document, comments included (its own printable set).
_UNREADABLE = re.compile("[^\t\n\r\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _reads_back(text: str, value: str, *, key: bool) -> bool:
    """True when ``text``, written as a mapping key (or as a mapping value), reads back as exactly ``value``.

    The two positions differ: a quoted key must fit on one line, and a line separator inside it does not.
    """
    try:
        back = yaml.safe_load(f"{text}: v" if key else f"k: {text}")
    except Exception:  # not only YAMLError: PyYAML raises ValueError for 2026-02-30 and AttributeError for a "!!timestamp x" tag
        return False
    if not isinstance(back, dict) or len(back) != 1:
        return False
    [(k, v)] = back.items()
    got, other = (k, v) if key else (v, k)
    # A date-looking value read back as a ``date`` is fine: its ``str`` is the same text (``checked: 2026-09-30``).
    return type(got) in (str, date) and str(got) == value and other == ("v" if key else "k")


def _scalar(value: str, *, key: bool = False) -> str:
    """``value`` as YAML text that reads back as exactly the same string: plain if possible, else quoted."""
    if _reads_back(value, value, key=key):
        return value
    single = "'" + value.replace("'", "''") + "'"
    if _reads_back(single, value, key=key):
        return single
    return yaml.safe_dump(value, default_style='"', allow_unicode=True, width=10**9).rstrip("\n")


def dump_pricing(table: PriceTable, header: str = "") -> str:
    """Serialise to YAML, regenerating a source/date comment above each model."""
    lines: list[str] = []
    if header:
        lines += [f"# {_UNREADABLE.sub('?', ln)}".rstrip() for ln in header.splitlines()]
        lines.append("")
    lines += ["schema: 1", "models:"]
    for name in sorted(table.models):
        p = table.models[name]
        lines.append("")
        comment = f"{name} | source: {p.source or 'n/a'} | checked: {p.checked or 'n/a'}"
        lines.append("  # " + " ".join(_UNREADABLE.sub("?", comment).split()))
        lines.append(f"  {_scalar(name, key=True)}:")
        if p.provider:
            lines.append(f"    provider: {_scalar(p.provider)}")
        for f in PRICE_FIELDS:
            val = _fmt(getattr(p, f), f in p.unverified_fields)
            if val is not None:
                lines.append(f"    {f}: {val}")
        if p.source:
            lines.append(f"    source: {_scalar(p.source)}")
        if p.checked:
            lines.append(f"    checked: {_scalar(p.checked)}")
        if p.note:
            lines.append(f"    note: {_scalar(p.note)}")
    if table.aliases:
        lines += ["", "aliases:"]
        for a in sorted(table.aliases):
            lines.append(f"  {_scalar(a, key=True)}: {_scalar(table.aliases[a])}")
    return "\n".join(lines) + "\n"
