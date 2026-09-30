"""Money helpers. No floats anywhere in cost math.

The canonical unit is the US cent, held as an integer. Per-token costs are far
below one cent, so the integer counts *micro-cents* ("ucents", 1 cent = 1_000_000
ucents): still an exact integer, still cents, just scaled so a single request
does not round to zero. ``to_cents`` rounds half-up to whole cents; ``format_usd``
prints dollars with two decimals, or four when the amount is not a whole cent, and
``format_usd_compact`` drops the fraction of a cent from $1 up.
"""

from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)

# Decimal money math runs in this context, never in the caller's. The default one keeps 28 significant digits (and a
# caller may have lowered that), so a product of a long price and a token count would be rounded before the one
# half-up rounding that is meant to happen at the end. With no precision limit, sums and products are exact.
EXACT_CONTEXT = Context(
    prec=MAX_PREC,
    Emax=MAX_EMAX,
    Emin=MIN_EMIN,
    rounding=ROUND_HALF_UP,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

UCENTS_PER_CENT = 1_000_000
UCENTS_PER_USD = 100 * UCENTS_PER_CENT
# 4 decimal places of a dollar = 0.01 cent
_UCENTS_PER_USD_4DP = UCENTS_PER_USD // 10_000


def _round_div(numerator: int, denominator: int) -> int:
    """Integer division rounding half away from zero."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def to_cents(ucents: int) -> int:
    """Whole cents, rounded half away from zero."""
    return _round_div(ucents, UCENTS_PER_CENT)


def cents_to_ucents(cents: int) -> int:
    return cents * UCENTS_PER_CENT


def usd_to_ucents(usd: Decimal | int | str) -> int:
    """Exact conversion of a dollar amount (Decimal/str/int, never float) to ucents."""
    if isinstance(usd, float):
        raise TypeError("dollar amounts must be Decimal, int or str, never float")
    with localcontext(EXACT_CONTEXT):
        d = usd if isinstance(usd, Decimal) else Decimal(str(usd))
        return int((d * UCENTS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))


def format_usd(ucents: int, places: int | None = None) -> str:
    """Format as dollars.

    ``places=None``: two decimals when the amount is a whole cent at 4-decimal
    precision, otherwise four (``$4.20``, ``$0.0042``). ``places=2|4`` forces it.
    """
    n4 = _round_div(abs(ucents), _UCENTS_PER_USD_4DP)  # hundredths of a cent
    if places is None:
        places = 2 if n4 % 100 == 0 else 4
    if places == 4:
        shown, scale = n4, 10_000
    elif places == 2:
        shown, scale = _round_div(abs(ucents), UCENTS_PER_CENT), 100
    else:
        raise ValueError("places must be None, 2 or 4")
    whole, frac = divmod(shown, scale)
    sign = "-" if ucents < 0 and shown else ""  # a negative amount that rounds to zero prints as zero
    return f"{sign}${whole:,}.{frac:0{places}d}"


def format_usd_compact(ucents: int) -> str:
    """Whole cents from $1 up, where a fraction of a cent no longer matters; below $1 ``format_usd`` decides."""
    return format_usd(ucents, 2 if abs(ucents) >= UCENTS_PER_USD else None)


def ratio_str(numerator: int, denominator: int) -> str:
    """'5.0x' style ratio, integer inputs rounded half-up like every other amount; '-' when undefined."""
    if denominator <= 0:
        return "-"
    tenths = _round_div(numerator * 10, denominator)
    whole, frac = divmod(abs(tenths), 10)
    return f"{'-' if tenths < 0 else ''}{whole}.{frac}x"
