"""The optional network step: re-read the official pricing pages and compare them with the price table.

This is the ONLY module in AgentFare that may import ``urllib.request``, ``http.client``, ``socket`` or ``ssl``,
and only ``agentfare prices --refresh`` reaches it (``tests/test_no_network.py`` enforces both). Nothing here ever
changes a price: each page is downloaded once, reduced to plain text, and every model is reported as ``confirmed``
(each verified table price is the number the first table of the page labels as that price), ``changed`` (a price
differs or is missing), ``not_found`` or ``error``. What that can and cannot prove is written down in
``commands/prices.py``.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from itertools import accumulate, islice

from . import __version__
from .pricing import MAX_PRICE, PRICE_FIELDS, ModelPrice, PriceTable
from .textsafe import clean_text

USER_AGENT = f"agentfare/{__version__} (local price check; no cookies, no tracking)"
MAX_PAGE_BYTES = 5 * 1024 * 1024  # the real pricing pages are 0.1 to 1.2 MB of HTML
TIMEOUT_SECONDS = 20  # per blocking socket operation
DEADLINE_SECONDS = 60  # for one whole download, so a server that trickles bytes cannot hold the command forever
MAX_REDIRECTS = 5
WINDOW_CHARS = 2000  # how far after a model's name its prices are looked for
NEARBY_LIMIT = 12  # numbers listed as "found nearby" when a price differs
WINDOW_PRICES = 64  # a row or window is never read past this many prices, nor a model past MAX_MENTIONS names: a hostile page stays linear
MAX_MENTIONS = 200
LEAD_CHARS = 40  # a name that opens a row, heading or list item has at most this much punctuation before it
MAX_DIGITS = 12  # a number on a page with more significant digits than this is not a price (and is never echoed)
MAX_PRICES = 50_000  # a page is read for at most this many prices and unit statements: a real one has a few hundred

CONFIRMED, CHANGED, NOT_FOUND, ERROR = "confirmed", "changed", "not_found", "error"

_TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/x-markdown"}
_PLAIN_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}


# ---- download ---------------------------------------------------------------------------
MAX_REASON_CHARS = 300


class FetchError(Exception):
    """A page could not be downloaded; the message is the reason shown to the user.

    It often holds words from the server (a status line, a header) or from the system, so it is made harmless here, once,
    for everything that prints it: control characters and newlines become ``?``, and it is cut at ``MAX_REASON_CHARS``."""

    def __init__(self, message: object = "") -> None:
        text = clean_text(str(message))
        super().__init__(text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 3] + "...")


@dataclass(frozen=True)
class Page:
    text: str  # plain text of the page (see extract_text)
    size: int  # bytes downloaded


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a few redirects, to http(s) only, never from https down to http, and (unless told otherwise) never to a host
    that is not public: a page on the internet cannot send this command to a service on the machine or its network."""

    max_redirections = MAX_REDIRECTS

    def __init__(self, allow_private_hosts: bool = False) -> None:
        self.allow_private_hosts = allow_private_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        old = urllib.parse.urlsplit(req.full_url).scheme
        try:
            new = urllib.parse.urlsplit(newurl).scheme
        except ValueError:
            new = ""
        if new not in ("http", "https") or (old == "https" and new == "http"):
            raise urllib.error.HTTPError(req.full_url, code, f"redirect to {new or 'an unsupported URL'} refused", headers, fp)
        _check_url(newurl)  # the same rules as for the first request, credentials included
        if not self.allow_private_hosts:
            _require_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(allow_private_hosts: bool = False) -> urllib.request.OpenerDirector:
    """Plain HTTP(S) only: no cookie jar, no file/ftp/data handlers. Proxy settings from the environment still apply."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),  # verifies certificates
        _SafeRedirects(allow_private_hosts),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


@dataclass(frozen=True)
class _Body:
    data: bytes
    content_type: str
    charset: str


def _check_url(url: str) -> None:
    """Raise FetchError unless ``url`` is one this module will request. urlsplit and the port check raise ValueError."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number in range
    except ValueError as exc:
        raise FetchError(f"not a valid URL ({exc})") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("only http and https URLs are fetched")
    if parts.username is not None or parts.password is not None:
        raise FetchError("a URL with a user name or password is not fetched (credentials are never sent)")
    if not url.isascii():
        raise FetchError("the URL has characters that are not ASCII; write them percent-encoded")


_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _is_public(address: str) -> bool:
    """True for an address on the public internet. Loopback, private, link-local, unspecified, multicast, reserved and shared
    (100.64.0.0/10) addresses are not, and neither is an IPv4 address carried inside an IPv6 one (``::ffff:127.0.0.1``, 6to4, NAT64)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # a zone id (fe80::1%eth0) is not part of the address
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            return _is_public(str(inner))
    return ip.is_global and not ip.is_multicast


def _require_public(url: str) -> None:
    """Raise FetchError when the host of ``url`` is, or resolves to, an address that is not public (see ``_is_public``).

    Every address the name resolves to counts: one private answer among public ones is enough to refuse. It runs in the download
    thread, because a resolver that never answers must not hold the command past its deadline."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        found = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:  # the name does not resolve: the same reason the download itself would give
        raise FetchError(_reason(exc)) from None
    for address in dict.fromkeys(str(info[4][0]) for info in found):
        if not _is_public(address):
            where = address if address == host else f"{host} ({address})"
            raise FetchError(f"refusing to fetch {where}: not a public address (loopback, private, link-local or unspecified); pass --allow-private-hosts to fetch it anyway")


def _download(url: str, cancelled: threading.Event, allow_private_hosts: bool = False) -> _Body:
    """Request ``url`` and read at most ``MAX_PAGE_BYTES`` of a text reply. Runs in a worker thread (see fetch)."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",  # not text/markdown: sites then answer with a cut-down view
            "Accept-Encoding": "identity",
        },
    )
    if not allow_private_hosts:
        _require_public(url)
    limit = MAX_PAGE_BYTES
    oversize = f"page is larger than {limit // 1024} KB, not read"
    try:
        with _opener(allow_private_hosts).open(request, timeout=TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type() if response.headers.get("Content-Type") else "text/html"
            if content_type not in _TEXT_TYPES:
                raise FetchError(f"not a text page (content type {content_type})")
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise FetchError(f"the server sent the page {encoding}-compressed although it was asked not to")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > limit:
                raise FetchError(oversize)
            chunks: list[bytes] = []
            size = 0
            while not cancelled.is_set() and (chunk := response.read1(64 * 1024)):
                size += len(chunk)
                if size > limit:
                    raise FetchError(oversize)
                chunks.append(chunk)
            if declared.isdigit() and size < int(declared) and not cancelled.is_set():  # read1 does not notice a connection cut short
                raise FetchError(f"the connection closed after {size} of {declared} bytes")
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
        exc.close()  # the error is also the reply, and holds the connection until it is closed
        raise FetchError(f"HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise FetchError(_reason(exc.reason)) from None
    except (OSError, http.client.HTTPException, ValueError) as exc:  # timeouts, resets, TLS failures, truncated bodies, bad host labels
        raise FetchError(_reason(exc)) from None
    return _Body(b"".join(chunks), content_type, charset)


def fetch(url: str, *, allow_private_hosts: bool = False) -> Page:
    """Download one page (at most ``MAX_PAGE_BYTES``, at most ``DEADLINE_SECONDS``) and return its text. Raises :class:`FetchError`.

    A host that is loopback, private, link-local or unspecified (or resolves to one) is refused, at the first request and at
    every redirect, unless ``allow_private_hosts``.

    The download runs in a daemon thread that this function stops waiting for after the deadline: a blocking read
    only has a per-operation timeout, so a server that answers one byte at a time (or a resolver that never does)
    would otherwise keep the command running for as long as it likes."""
    _check_url(url)
    cancelled = threading.Event()
    outcome: list[_Body | BaseException] = []

    def work() -> None:
        try:
            outcome.append(_download(url, cancelled, allow_private_hosts))
        except BaseException as exc:  # handed to the caller below, whatever it is
            outcome.append(exc)

    worker = threading.Thread(target=work, name="agentfare-download", daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    if not outcome:
        cancelled.set()
        raise FetchError(f"download took longer than {DEADLINE_SECONDS} s")
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    body = outcome[0]
    try:
        text = body.data.decode(body.charset, errors="replace")
    except LookupError:  # a charset name Python does not know
        text = body.data.decode("utf-8", errors="replace")
    return Page(text=extract_text(text, body.content_type), size=len(body.data))


# ---- page text ----------------------------------------------------------------------------
_BLOCK = frozenset(
    "address article aside blockquote body br caption dd details div dl dt fieldset figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section summary table tbody tfoot thead ul".split()
)
_SKIP = frozenset(("script", "style", "noscript", "template", "svg"))
_INVISIBLE = {0x00AD: None, 0x200B: None, 0x200C: None, 0x200D: None, 0x2060: None, 0xFEFF: None}


class _TextExtractor(HTMLParser):
    """HTML to lines of text. A table row becomes ONE line with its cells joined by ' | ', so a model's name and
    its prices stay together even when every cell wraps its content in divs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._parts: list[str] = []
        self._skip = 0
        self._in_row = False
        self._cells = 0

    def _flush(self) -> None:
        self.lines.append("".join(self._parts))
        self._parts.clear()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif self._skip:
            return
        elif tag == "tr":
            self._flush()
            self._in_row, self._cells = True, 0
        elif tag in ("td", "th"):
            if not self._in_row:  # HTML lets <tr> be left out
                self._flush()
                self._in_row, self._cells = True, 0
            if self._cells:
                self._parts.append(" | ")
            self._cells += 1
        elif tag in _BLOCK:
            if tag == "table":
                self._in_row = False
            if self._in_row:
                self._parts.append(" ")
            else:
                self._flush()
        else:
            self._parts.append(" ")  # <a>Name</a><span>text</span> is two words, not one

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag in ("tr", "table"):
            self._in_row = False
            self._flush()
        elif tag in _BLOCK:
            if self._in_row:
                self._parts.append(" ")
            else:
                self._flush()
        else:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


_MD_LINK = re.compile(r"\[([^\[\]]{0,200})\]\([^)\s]{0,500}\)")  # bounded, so a page of '[' cannot make the scan quadratic
_RULE_CHARS = frozenset("|:- \t")


def _clean_line(line: str) -> str:
    line = " ".join(line.translate(_INVISIBLE).split())
    if "-" in line and _RULE_CHARS.issuperset(line):  # the |---|---| row under a markdown table header
        return ""
    return _MD_LINK.sub(r"\1", line).replace("**", "").replace("__", "").replace("`", "")


def extract_text(body: str, content_type: str = "text/html") -> str:
    """Visible text of an HTML page, or of a markdown/plain-text one: scripts, styles and tags dropped, entities
    decoded, one line per block (a table row is one line)."""
    if content_type in _PLAIN_TYPES:
        lines = unescape(body).splitlines()
    else:
        parser = _TextExtractor()
        parser.feed(body)
        parser.close()
        lines = parser.lines
    return "\n".join(line for line in map(_clean_line, lines) if line)


# ---- prices in the text ---------------------------------------------------------------------
@dataclass(frozen=True)
class Price:
    pos: int
    value: Decimal  # USD per 1M tokens
    bare: bool = False  # a number in a table cell with no currency sign
    line: int = 0  # which line of the page text it is on
    field: str | None = None  # the table price the page labels it as ("input", "cache_write"...), None when it says nothing


# A digit run is bounded in the pattern itself, so a page that is one endless number costs one failed match, not five million
# steps of backtracking: a number with more digits than a price can have does not match at all (and is never printed).
_NUM = rf"(?:\d{{1,3}}(?:,\d{{3}}){{1,4}}(?:\.\d{{1,{MAX_DIGITS}}})?|\d{{1,{MAX_DIGITS}}}(?:\.\d{{1,{MAX_DIGITS}}})?|\.\d{{1,{MAX_DIGITS}}})(?![,.]?\d)"
_MONEY = re.compile(
    rf"(?<![A-Za-z0-9])(?:US\$|\$|USD)[ \t]?(?P<num>{_NUM})"  # $1.50  US$1.50  USD 1.50
    rf"|(?<![\w.,])(?P<num2>{_NUM})[ \t]?(?:USD|US\$)\b"  # 1.50 USD
    rf"|(?<=\|)[ \t]*(?P<cell>{_NUM})[ \t]*(?=\||$)",  # a bare number filling a table cell
    re.M | re.I,
)
_OTHER_UNIT = (
    r"(?:/|per)\s*(?:\d[\d,]*\s*)?(?:(?:grounded|search|web|maps?|google)\s+)?(?:hours?|hrs?|days?|months?|minutes?|mins?|seconds?|secs?|requests?|"
    r"quer(?:y|ies)|searche?s?|prompts?|images?|calls?|pages?|gb)\b"
)
_PER_OTHER = re.compile(rf"\s*(?:USD\s*)?{_OTHER_UNIT}", re.I)
_TOKEN_UNIT = r"(?:/|\bper\b|\|)\s*(?:(?P<n>\d[\d,]*)\s*)?(?P<u>million|thousand|mtoks?|ktoks?|m|k|tokens?)\b"
_PER_TOKENS = re.compile(rf"\s*(?:USD\s*)?{_TOKEN_UNIT}(?P<rest>.*)", re.I)
_DECLARATION = re.compile(_TOKEN_UNIT, re.I)
_TOKENS_WORD = re.compile(r"\s*(?:(?:input|output|cached|cache)\s+)?tokens?", re.I)
_LONG_UNITS = {"million", "thousand", "mtok", "mtoks", "ktok", "ktoks"}


def _unit(match: re.Match[str]) -> str:
    """'M' (per million tokens), 'K' (per thousand) or '' (a rate this module does not read)."""
    count = int((match.groupdict().get("n") or "1").replace(",", ""))
    unit = match["u"].lower()
    if unit in ("million", "m", "mtok", "mtoks"):
        return "M" if count == 1 else ""
    if unit in ("thousand", "k", "ktok", "ktoks"):
        return "K" if count == 1 else ""
    return {1_000_000: "M", 1_000: "K"}.get(count, "")  # "per 1,000,000 tokens"; a bare "per token" is not read


def _inline_unit(tail: str) -> str | None:
    """The unit written right after a number: 'M', 'K', '' (a price per hour, request, image..., or a unit not read)
    or None when the number states none."""
    if _PER_OTHER.match(tail):
        return ""
    found = _PER_TOKENS.match(tail)
    if not found:
        return None
    rest = found["rest"]
    if word := _TOKENS_WORD.match(rest):
        rest = rest[word.end() :]
    if _PER_OTHER.match(rest):  # "$1.00 / 1M tokens per hour" is a storage fee
        return ""
    unit = _unit(found)
    if not unit and tail.lstrip().startswith("|"):  # "| 200K prompt tokens" is the next cell, not this price's unit
        return None
    return unit


def find_prices(text: str) -> list[Price]:
    """Every dollar amount that the page ties to a token unit, converted to USD per 1M tokens.

    A unit written next to the number wins ("$2 / MTok", "$0.002 per 1K tokens"). Otherwise the nearest earlier
    statement of a token unit on the page applies ("Prices per 1M tokens." above a table). With neither, the
    number is ignored: it could be a price per request. Amounts per hour, per request, per image... are ignored."""
    declared = list(islice((
        (m.start(), unit)
        for m in _DECLARATION.finditer(text)
        if (m["n"] or m["u"].lower() in _LONG_UNITS) and (unit := _inline_unit(text[m.start() : m.start() + 60].split("\n", 1)[0]))
    ), MAX_PRICES))
    starts = [pos for pos, _ in declared]
    found: list[Price] = []
    for m in _MONEY.finditer(text):
        if len(found) >= MAX_PRICES:  # a hostile page: the rest is not read
            break
        raw = m["num"] or m["num2"] or m["cell"]
        unit = _inline_unit(text[m.end() : m.end() + 60].split("\n", 1)[0])
        if unit is None:
            earlier = bisect_left(starts, m.start()) - 1
            unit = declared[earlier][1] if earlier >= 0 else ""
        if not unit:
            continue
        value = Decimal(raw.replace(",", ""))
        if len(value.as_tuple().digits) > MAX_DIGITS:  # 1.2345678901234: no price has that many digits
            continue
        per_mtok = value * 1000 if unit == "K" else value
        if per_mtok <= MAX_PRICE:  # above what the price table itself accepts (pricing.MAX_PRICE): not a price
            found.append(Price(m.start(), per_mtok, bare=bool(m["cell"])))
    return _label(text, found)


# ---- which price is which ---------------------------------------------------------------------
# A number that sits in the right window is not yet the right price: a row also shows the 1-hour write, the
# long-context rate and the Batch rate, and a price halved or doubled is exactly one of them. So each number is
# tied to the label the page gives it: its column header, the first cell of its row, the words just before it or
# the line above it.
_TIER = re.compile(r"\b(?:batch|flex|priority|fast|ultrafast|long[- ]context|above|over|beyond|under)\b|[<>\u2264\u2265]", re.I)
_WRITE = re.compile(r"\bwrit|\bcreat", re.I)
_HOUR = re.compile(r"\b(?:1|one)[\s-]*(?:h|hr|hour)s?\b|\b60[\s-]*min", re.I)
_CACHE = re.compile(r"\bcach|\bhits?\b|\brefresh", re.I)
_MISS = re.compile(r"\bmiss", re.I)  # "Input (cache miss)" is the ordinary input price
_INPUT, _OUTPUT = re.compile(r"\binput", re.I), re.compile(r"\boutput", re.I)
_UNIT_LINE = re.compile(r"(?:text |per (?:1m|million) )?tokens?", re.I)  # "Tokens" between a label and its price
HEADER_LINES = 60  # how far above a row its column headers are looked for
LABEL_CHARS = 30  # words before a price count as its label only when they are this short
ALONE_CHARS = 32  # a price on a line of its own is at most this long
LABEL_LINE_CHARS = 40  # a line above a price names it only when it is this short (a sentence names nothing)
MAX_ROW_CHARS = 1500  # a table row is a few hundred characters: a longer line is not labelled, which keeps a hostile page linear


def field_of(label: str) -> str | None:
    """The table price a label names: 'Input price' is input, '5m writes' cache_write, 'Hits and refreshes' cached_input.
    None when it names none, or two ('Input and output'), or a tier or long-context rate the table does not carry."""
    if _TIER.search(label):
        return None
    if _MISS.search(label):
        return "input"
    if _WRITE.search(label):
        return "cache_write_1h" if _HOUR.search(label) else "cache_write"
    if _CACHE.search(label):
        return "cached_input"
    reads_input, reads_output = bool(_INPUT.search(label)), bool(_OUTPUT.search(label))
    if reads_input != reads_output:
        return "input" if reads_input else "output"
    return None


def _header(lines: list[str], i: int, priced: set[int]) -> list[str] | None:
    """Cells of the column headers of the row at line ``i``: the nearest line above with the same number of cells and
    no price of its own, if it names any price."""
    shape = lines[i].count("|")
    for j in range(i - 1, max(-1, i - HEADER_LINES - 1), -1):
        if lines[j].count("|") == shape and j not in priced:
            cells = lines[j].split("|")
            return cells if any(field_of(c) for c in cells) else None
    return None


def _above(lines: list[str], i: int, priced: set[int]) -> str | None:
    """The price named by the line above line ``i``, or by the one above that when the line between only says 'Tokens'."""
    for j in (i - 1, i - 2):
        if j < 0 or j in priced or len(lines[j]) > LABEL_LINE_CHARS:
            return None
        if field := field_of(lines[j]):
            return field
        if not _UNIT_LINE.fullmatch(lines[j]):
            return None
    return None


def _line_starts(lines: list[str]) -> list[int]:
    return list(accumulate((len(line) + 1 for line in lines), initial=0))


def _label(text: str, prices: list[Price]) -> list[Price]:
    """``prices`` with their line number and the table price the page labels each as."""
    lines = text.split("\n")
    starts = _line_starts(lines)
    at = [bisect_right(starts, p.pos) - 1 for p in prices]
    priced, per_line = set(at), Counter(at)
    headers: dict[int, list[str] | None] = {}
    tagged = []
    for p, i in zip(prices, at):
        line, off = lines[i], p.pos - starts[i]
        field = None
        if len(line) > MAX_ROW_CHARS:
            tagged.append(replace(p, line=i))
            continue
        if "|" in line:
            cell = line.count("|", 0, off)
            if i not in headers:
                headers[i] = _header(lines, i, priced)
            header = headers[i]
            first = next((c for c in line.split("|")[:cell] if c.strip()), "")
            field = (header and field_of(header[cell])) or field_of(first)
        before = line[line.rfind("|", 0, off) + 1 : off].strip(" :-\u2013\u2014=")
        if not field and 0 < len(before) <= LABEL_CHARS:
            field = field_of(before)
        if not field and per_line[i] == 1 and len(line) <= ALONE_CHARS:
            field = _above(lines, i, priced)
        tagged.append(replace(p, line=i, field=field))
    return tagged


# ---- model names in the text -----------------------------------------------------------------
@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    model: str
    leads: bool  # nothing but punctuation before it on its line: a table row, heading or list item about this model


def _display_pattern(model_id: str) -> str:
    """Spellings of an id: 'claude-sonnet-5-5' is also 'Claude Sonnet 5.5', 'gemini-3.5-flash-lite' 'Gemini 3.5 Flash-Lite'."""
    tokens = model_id.split("-")
    out = [re.escape(tokens[0])]
    for prev, tok in zip(tokens, tokens[1:]):
        out.append(r"[.\-\s]" if prev.isdigit() and tok.isdigit() else r"[\-\s_]+")
        out.append(re.escape(tok))
    return "".join(out)


def _mentions(text: str, table: PriceTable) -> list[_Mention]:
    """Where each table model is named (its id, display name or an alias), longest name winning where they overlap:
    'Gemini 3.5 Flash' inside 'Gemini 3.5 Flash Lite' is not a mention of the plain Flash model."""
    found: list[_Mention] = []
    starts = _line_starts(text.split("\n"))
    for model in table.models:
        names = [_display_pattern(model)] + [re.escape(a) for a, target in table.aliases.items() if target == model]
        pattern = re.compile(rf"(?<![\w.\-])(?:{'|'.join(names)})(?!\w)(?![.\-]\w)", re.I)
        for m in pattern.finditer(text):
            line_start = starts[bisect_right(starts, m.start()) - 1]
            leads = m.start() - line_start <= LEAD_CHARS and not re.search(r"\w", text[line_start : m.start()])
            found.append(_Mention(m.start(), m.end(), model, leads))
    found.sort(key=lambda m: (m.start, -m.end, m.model))
    kept: list[_Mention] = []
    reach = -1
    for m in found:
        if m.end > reach:
            kept.append(m)
            reach = m.end
    return kept


# ---- comparing ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldCheck:
    field: str
    table: Decimal
    found: bool  # the page shows this table value where it labels this price (or, with no labels on it, among the numbers)
    page: Decimal | None = None  # what the page labels as this price, when it labels one


@dataclass(frozen=True)
class ModelCheck:
    model: str
    status: str  # CONFIRMED | CHANGED | NOT_FOUND | ERROR
    detail: str = ""  # why, for NOT_FOUND, ERROR and a CHANGED that no single price explains
    fields: tuple[FieldCheck, ...] = ()  # every verified table price and whether the page shows it
    nearby: tuple[Decimal, ...] = ()  # numbers the page shows next to the model, for a human to compare


@dataclass(frozen=True)
class _Read:
    """A page as read: its text, the prices in it (in order of position) and the model names in it."""

    text: str
    prices: list[Price]
    positions: list[int]
    mentions: list[_Mention]


def _window(read: _Read, index: int) -> list[Price]:
    """Prices after mention ``index``. A table row that already carries prices (bare cells included) is the whole
    window; otherwise it runs to the next line that starts with another model's name, or WINDOW_CHARS."""
    me = read.mentions[index]
    row_end = read.text.find("\n", me.end)
    row_end = len(read.text) if row_end < 0 else row_end
    first = bisect_left(read.positions, me.end)
    if row := read.prices[first : min(bisect_left(read.positions, row_end), first + WINDOW_PRICES)]:
        return row
    end = min(len(read.text), me.end + WINDOW_CHARS)
    if first == len(read.prices) or read.positions[first] >= end:
        return []
    for n in range(index + 1, len(read.mentions)):
        other = read.mentions[n]
        if other.start >= end:
            break
        if other.model != me.model and other.leads:
            end = other.start
            break
    return [p for p in read.prices[first : min(bisect_left(read.positions, end), first + WINDOW_PRICES)] if not p.bare]


def _block(window: list[Price]) -> list[Price]:
    """One table's prices: a window up to the point where a label comes round again on a later line, which starts the
    next tier (Batch, Fast...) or the next model. Only the first table is the model's standard price: a price halved
    or doubled equals one of the others."""
    first_line: dict[str, int] = {}
    for n, p in enumerate(window):
        if p.field is not None and first_line.setdefault(p.field, p.line) != p.line:
            return window[:n]
    return window


def _said(block: list[Price]) -> dict[str, Price]:
    """What the page says each price is: the first number under each label (a second one on the same row is a
    long-context or other-tier rate)."""
    said: dict[str, Price] = {}
    for p in block:
        if p.field is not None:
            said.setdefault(p.field, p)
    return said


def _match(wanted: dict[str, Decimal], values: list[Decimal]) -> dict[str, bool]:
    """Which table prices are among ``values``. Each number can vouch for one price only, so a change of one price to
    the value of another column of the same row does not go unnoticed."""
    left = Counter(values)
    found: dict[str, bool] = {}
    for field, value in wanted.items():
        found[field] = left[value] > 0
        if found[field]:
            left[value] -= 1
    return found


def _check_model(read: _Read, price: ModelPrice) -> ModelCheck:
    """Compare a model's verified prices with the first table the page shows for it.

    Numbers with labels are compared price by price. Numbers without any label cannot be told apart, so they confirm
    only when they are exactly the table's prices: with a number to spare, one of them may be another column's."""
    own = [i for i, m in enumerate(read.mentions) if m.model == price.model]
    if not own:
        return ModelCheck(price.model, NOT_FOUND, "not named on this page (it may go by another name there, or the page draws its content with JavaScript)")
    wanted = {f: v for f in PRICE_FIELDS if (v := getattr(price, f)) is not None}
    blocks = [block for i in own[:MAX_MENTIONS] if (block := _block(_window(read, i)))]
    if not blocks:
        return ModelCheck(price.model, NOT_FOUND, "named on this page, but no price is shown next to it")
    said = [_said(block) for block in blocks]
    labelled = [n for n, s in enumerate(said) if s]
    pick = next((n for n in labelled if wanted.keys() <= said[n].keys()), labelled[0] if labelled else 0)  # a summary card that lacks a price is passed over
    values = [p.value for p in blocks[pick]]
    nearby = tuple(dict.fromkeys(values))[:NEARBY_LIMIT]
    if said[pick]:
        checks = tuple(
            FieldCheck(f, v, f in said[pick] and said[pick][f].value == v, said[pick][f].value if f in said[pick] else None) for f, v in wanted.items()
        )
        return ModelCheck(price.model, CONFIRMED if checks and all(c.found for c in checks) else CHANGED, fields=checks, nearby=nearby)
    matched = _match(wanted, values)
    checks = tuple(FieldCheck(f, v, matched[f]) for f, v in wanted.items())
    if checks and all(c.found for c in checks):
        if Counter(values) == Counter(wanted.values()):
            return ModelCheck(price.model, CONFIRMED, fields=checks, nearby=nearby)
        detail = f"the page shows {len(values)} numbers next to the model and labels none of them, so the table's prices cannot be told apart from the others"
        return ModelCheck(price.model, CHANGED, detail, checks, nearby)
    return ModelCheck(price.model, CHANGED, fields=checks, nearby=nearby)


def check_page(text: str, table: PriceTable, models: Iterable[str]) -> dict[str, ModelCheck]:
    """Compare ``models`` (ids in ``table``) with the plain text of a pricing page."""
    prices = find_prices(text)
    read = _Read(text, prices, [p.pos for p in prices], _mentions(text, table))
    return {m: _check_model(read, table.models[m]) for m in models}


# ---- the whole refresh ------------------------------------------------------------------------------
@dataclass(frozen=True)
class PageOutcome:
    url: str
    size: int = 0
    error: str = ""  # empty when the page was read
    models: tuple[str, ...] = ()  # the models whose source is this page

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class Refresh:
    checks: dict[str, ModelCheck]
    pages: list[PageOutcome]

    @property
    def all_failed(self) -> bool:
        """No model could be checked: every download failed, or no model names a source page."""
        return bool(self.checks) and all(c.status == ERROR for c in self.checks.values())


def page_label(url: str) -> str:
    """Host and path for progress lines: never the query string, fragment or credentials."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "?"
    return clean_text(f"{parts.hostname or '?'}{parts.path}")


def refresh(
    table: PriceTable,
    model_ids: Iterable[str],
    progress: Callable[[str, PageOutcome | None], None] = lambda label, outcome: None,
    *,
    allow_private_hosts: bool = False,
) -> Refresh:
    """Fetch each distinct source page once and check the given models against it.

    ``progress(label, None)`` is called before a download and ``progress(label, outcome)`` after it."""
    by_url: dict[str, list[str]] = {}
    checks: dict[str, ModelCheck] = {}
    for model in sorted(model_ids):
        url = table.models[model].source.split("#", 1)[0].strip()
        if url:
            by_url.setdefault(url, []).append(model)
        else:
            checks[model] = ModelCheck(model, ERROR, "the price table names no source page for it")
    pages: list[PageOutcome] = []
    for url in sorted(by_url):
        models, label = tuple(by_url[url]), page_label(url)
        progress(label, None)
        try:
            page = fetch(url, allow_private_hosts=allow_private_hosts)
        except FetchError as exc:
            outcome = PageOutcome(url, error=str(exc), models=models)
            checks.update({m: ModelCheck(m, ERROR, f"{label}: {exc}") for m in models})
        else:
            outcome = PageOutcome(url, size=page.size, models=models)
            checks.update(check_page(page.text, table, models))
        pages.append(outcome)
        progress(label, outcome)
    return Refresh(dict(sorted(checks.items())), pages)
