"""`agentfare prices`: the offline table, and `--refresh` against pages served by a local http.server (no real network)."""

from __future__ import annotations

import gc
import gzip
import http.server
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from agentfare import pricefetch, pricing
from agentfare.cli import app
from agentfare.pricing import PRICE_FIELDS, builtin_path, load_pricing, parse_pricing

CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"  # timezone UTC, so "today" is 2026-09-30 at the frozen time
NOW = "2026-09-30T12:00:00Z"
TODAY = "2026-09-30"
OLD = "2026-01-01"


# ---- a local stand-in for the provider pages ---------------------------------------------------------
class Site:
    """A web server on 127.0.0.1 that serves canned pages and records every request it gets."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.hits: list[tuple[str, dict[str, str]]] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
                path = self.path.split("?", 1)[0]
                site.hits.append((path, {k.lower(): v for k, v in self.headers.items()}))
                status, headers, body = site.routes.get(path, (404, {"Content-Type": "text/plain"}, b"not here"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def page(self, path: str, body: str | bytes, *, status: int = 200, content_type: str = "text/html; charset=utf-8", **headers: str) -> str:
        data = body.encode() if isinstance(body, str) else body
        self.routes[path] = (status, {"Content-Type": content_type, **headers}, data)
        return self.base + path

    def count(self, path: str) -> int:
        return sum(1 for p, _ in self.hits if p == path)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def site() -> Iterator[Site]:
    s = Site()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def direct_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


# ---- helpers ------------------------------------------------------------------------------------------
def entry(source: str, input: Any = 2.0, cached: Any = 0.2, output: Any = 10.0, **extra: Any) -> dict[str, Any]:
    body = {"provider": "acme", "input": input, "cached_input": cached, "output": output, "source": source, "checked": OLD}
    return {**body, **extra}


def write_table(path: Path, models: dict[str, dict[str, Any]], aliases: dict[str, str] | None = None) -> Path:
    doc: dict[str, Any] = {"schema": 1, "models": models}
    if aliases:
        doc["aliases"] = aliases
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    return d


def run(home: Path, *args: str, private: bool = True) -> Any:
    """``agentfare prices``. The pages these tests fetch are served from 127.0.0.1, which a refresh refuses unless it is told
    (``--allow-private-hosts``, added here to every ``--refresh`` unless ``private=False``)."""
    extra = ["--allow-private-hosts"] if private and "--refresh" in args else []
    return CliRunner().invoke(app, ["--config", str(CONFIG), "--home", str(home), "--no-ingest", "prices", *args, *extra], env={"AGENTFARE_NOW": NOW})


def flat(text: str) -> str:
    """Prose is wrapped at 80 columns; compare it with the line breaks gone."""
    return " ".join(text.split())


def results(home: Path, *args: str) -> dict[str, dict[str, Any]]:
    out = run(home, "--refresh", "--json", *args)
    return {r["model"]: r for r in json.loads(out.stdout)["results"]}


def rows_html(rows: dict[str, tuple[str, ...]], note: str = "Prices per 1M tokens.") -> str:
    body = "".join(
        f"<tr><td><div>{name}</div><span>The model</span></td>" + "".join(f"<td><div>${v}</div></td>" for v in values) + "</tr>"
        for name, values in rows.items()
    )
    return (
        "<html><head><title>Pricing</title><style>.a{content:'$77.77'}</style>"
        "<script>var fake = '$99.99 / MTok';</script></head><body><h1>Pricing</h1>"
        f"<p>{note}</p><table><tr><th>Model</th><th>Input</th><th>Cached</th><th>Output</th></tr>{body}</table></body></html>"
    )


# ---- the offline table ---------------------------------------------------------------------------------
def test_builtin_table_lists_every_model_with_status_source_host_and_date(home: Path) -> None:
    table = load_pricing(path=builtin_path())
    out = run(home)
    assert out.exit_code == 0
    assert f"built-in table ({builtin_path()})" in flat(out.stdout)
    for name in table.models:
        assert name in out.stdout
    for host in {p.source.split("/")[2] for p in table.models.values()}:
        assert host in out.stdout
    assert "verified" in out.stdout and "checked" in out.stdout
    assert all(p.checked in out.stdout for p in table.models.values())
    assert "USD per 1M tokens" in out.stdout
    assert not home.joinpath("agentfare.db").exists()  # a price listing does not touch the database


def test_override_file_is_named_in_the_header(home: Path) -> None:
    override = write_table(home / "pricing.yaml", {"m-1": entry("https://example.com/p")})
    out = run(home)
    assert f"override file {override} (it replaces the built-in table)" in flat(out.stdout)
    assert "m-1" in out.stdout and "gpt-6.1-sol" not in out.stdout


def test_unverified_models_get_a_warning_block_naming_them(home: Path) -> None:
    write_table(
        home / "pricing.yaml",
        {
            "good": entry("https://example.com/p"),
            "partial": entry("https://example.com/p", cached="unverified"),
            "unknown": entry("https://example.com/p", input="unverified", cached="unverified", output="unverified"),
        },
    )
    out = run(home)
    warning = out.stdout.split("warning:", 1)[1]
    assert "partial: cached-input price is unverified" in warning
    assert "unknown: price is unverified" in warning
    assert "good:" not in warning
    rows = {line.split()[0]: line for line in out.stdout.splitlines() if line.startswith("  ") and line.split()[0] in ("good", "partial", "unknown")}
    assert "verified" in rows["good"].split() and "partial" in rows["partial"].split() and "unverified" in rows["unknown"].split()
    assert "?" in rows["unknown"].split() and "?" in rows["partial"].split()


def test_unverified_flag_lists_only_unverified_and_partial_models(home: Path) -> None:
    write_table(
        home / "pricing.yaml",
        {"good": entry("https://example.com/p"), "partial": entry("https://example.com/p", cached="unverified"), "unknown": entry("https://example.com/p", input="unverified")},
    )
    out = run(home, "--unverified", "--json")
    assert [m["model"] for m in json.loads(out.stdout)["models"]] == ["partial", "unknown"]
    text = run(home, "--unverified").stdout
    assert "partial" in text and "unknown" in text
    assert not any(line.startswith("  good") for line in text.splitlines())


def test_all_verified_says_so(home: Path) -> None:
    write_table(home / "pricing.yaml", {"good": entry("https://example.com/p")})
    out = run(home, "--unverified")
    assert "every model in the table has a verified price" in out.stdout
    assert not any(line.startswith("  good") for line in out.stdout.splitlines())
    assert "warning" not in out.stdout


def test_json_has_stable_keys_and_integer_money(home: Path) -> None:
    write_table(
        home / "pricing.yaml",
        {
            "tiny": entry("https://example.com/p", input=0.1, cached=0.0625, output=0.5),
            "part": entry("https://example.com/p", cached="unverified", cache_write=12.5),
        },
        aliases={"tiny-dated": "tiny"},
    )
    first = run(home, "--json")
    assert first.exit_code == 0 and first.stderr == ""
    data = json.loads(first.stdout)  # nothing but JSON on stdout
    assert list(data) == ["pricing", "unit", "models", "warnings"]
    assert data["pricing"] == {"source": "override", "path": str(home / "pricing.yaml")}
    part, tiny = data["models"]  # sorted by id
    assert list(tiny) == ["model", "provider", "status", "unverified_fields", "prices_per_mtok", "source", "source_host", "checked", "note", "aliases"]
    assert list(tiny["prices_per_mtok"]) == list(PRICE_FIELDS)
    assert tiny["prices_per_mtok"]["cached_input"] == {"usd": "0.0625", "cents": 6, "ucents": 6_250_000}
    assert tiny["prices_per_mtok"]["input"] == {"usd": "0.10", "cents": 10, "ucents": 10_000_000}
    assert tiny["prices_per_mtok"]["cache_write"] is None and tiny["aliases"] == ["tiny-dated"]
    assert tiny["status"] == "verified" and tiny["source_host"] == "example.com" and tiny["checked"] == OLD
    assert part["status"] == "partially unverified" and part["unverified_fields"] == ["cached_input"]
    assert part["prices_per_mtok"]["cached_input"] is None and part["prices_per_mtok"]["cache_write"]["ucents"] == 1_250_000_000
    assert [w["model"] for w in data["warnings"]] == ["part"]
    for money in (v for v in tiny["prices_per_mtok"].values() if v):
        assert isinstance(money["cents"], int) and isinstance(money["ucents"], int)
    assert run(home, "--json").stdout == first.stdout  # deterministic


def test_output_is_plain_text_that_fits_80_columns(home: Path) -> None:
    out = run(home)
    assert "\x1b" not in out.stdout
    # a path is never broken to fit, so the lines that carry the price file's path may be longer
    assert [line for line in out.stdout.splitlines() if len(line) > 80 and "pricing.yaml" not in line] == []


def test_the_help_states_the_real_limits() -> None:
    from agentfare.commands.prices import HELP

    assert f"at most {pricefetch.MAX_PAGE_BYTES // 2**20} MB and {pricefetch.DEADLINE_SECONDS} s per page" in HELP
    assert "ONLY part of AgentFare that uses the network" in HELP and "cannot verify" in HELP


def test_write_needs_refresh(home: Path) -> None:
    out = run(home, "--write")
    assert out.exit_code == 2 and "--write needs --refresh" in out.stderr


def test_listing_never_touches_the_network(home: Path, site: Site) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(site.page("/p", rows_html({"m-1": ("2.00", "0.20", "10.00")})))})
    assert run(home).exit_code == 0 and run(home, "--json").exit_code == 0
    assert site.hits == []


# ---- --refresh: statuses ---------------------------------------------------------------------------------
def build_mixed(home: Path, site: Site, aliases: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """One model per outcome, each on its own page."""
    site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    site.page("/beta", rows_html({"Beta 2": ("2.50", "0.20", "10.00")}))
    site.page("/gamma", "<html><body><div id=root></div><script>render({'gamma-3': {input: 2.0}})</script></body></html>")
    site.page(
        "/eps.md",
        "# Pricing\n\nPrices are per 1M tokens.\n\n| Model | Input | Cached | Output |\n| --- | --- | --- | --- |\n| **eps-5** | 2.00 | 0.20 | 10.00 |\n",
        content_type="text/markdown; charset=utf-8",
    )
    site.page("/zeta", "<html><body><p>zeta-6 costs $0.002 per 1K tokens of input and $0.01 per 1K tokens of output.</p></body></html>")
    site.page("/naming", "<html><body><p>eta-7 is a reviewer model with no price list.</p></body></html>")
    models = {
        "alpha-1": entry(site.base + "/alpha"),
        "beta-2": entry(site.base + "/beta"),
        "gamma-3": entry(site.base + "/gamma"),
        "delta-4": entry(site.base + "/missing"),
        "eps-5": entry(site.base + "/eps.md"),
        "zeta-6": entry(site.base + "/zeta", cached=None),
        "eta-7": entry(site.base + "/naming", input="unverified", cached="unverified", output="unverified"),
    }
    write_table(home / "pricing.yaml", models, aliases)
    return results(home)


def test_each_model_gets_confirmed_changed_not_found_or_error(home: Path, site: Site) -> None:
    got = build_mixed(home, site)
    status = {m: r["status"] for m, r in got.items()}
    assert status == {
        "alpha-1": "confirmed",  # <table> HTML whose cells wrap their content in divs; the script and style prices are ignored
        "beta-2": "changed",  # named "Beta 2" on the page, input differs
        "gamma-3": "not_found",  # drawn by JavaScript: the price is only inside a script
        "delta-4": "error",  # HTTP 404
        "eps-5": "confirmed",  # markdown table with bare numbers under a "per 1M tokens" statement
        "zeta-6": "confirmed",  # 0.002 per 1K tokens is 2.00 per 1M
        "eta-7": "not_found",  # named, no price next to it
    }
    assert "HTTP 404" in got["delta-4"]["detail"] and "127.0.0.1" in got["delta-4"]["detail"]
    assert "no price is shown next to it" in got["eta-7"]["detail"]


def test_changed_shows_table_value_and_the_numbers_found_nearby(home: Path, site: Site) -> None:
    got = build_mixed(home, site)
    beta = got["beta-2"]
    assert beta["prices_per_mtok"]["input"] == {"usd": "2.00", "cents": 200, "ucents": 200_000_000, "found_on_page": False, "page_price": {"usd": "2.50", "cents": 250, "ucents": 250_000_000}}
    assert beta["prices_per_mtok"]["cached_input"]["found_on_page"] and beta["prices_per_mtok"]["output"]["found_on_page"]
    assert [n["usd"] for n in beta["nearby_per_mtok"]] == ["2.50", "0.20", "10.00"]
    text = run(home, "--refresh").stdout
    assert "changed?" in next(ln for ln in text.splitlines() if ln.startswith("beta-2"))
    assert "not on the page: input 2.00 (page shows 2.50); numbers near the model: 2.50, 0.20, 10.00" in flat(text)


def test_refresh_text_output_lists_every_model_and_a_summary(home: Path, site: Site) -> None:
    build_mixed(home, site)
    out = run(home, "--refresh")
    assert out.exit_code == 0 and "\x1b" not in out.stdout
    for name in ("alpha-1", "beta-2", "gamma-3", "delta-4", "eps-5", "zeta-6", "eta-7"):
        assert any(line.startswith(name) for line in out.stdout.splitlines())
    assert "3 confirmed, 1 changed?, 2 not-found, 1 error" in out.stdout
    assert "needs a manual look: beta-2, delta-4, eta-7, gamma-3" in flat(out.stdout)
    assert "prices are never edited automatically" in flat(out.stdout)
    assert "eta-7: price is unverified" in flat(out.stdout)  # the warning block follows the results
    assert [line for line in out.stdout.splitlines() if len(line) > 80 and "/" not in line] == []


def test_a_refresh_changes_nothing_on_disk_without_write(home: Path, site: Site) -> None:
    build_mixed(home, site)
    before = (home / "pricing.yaml").read_bytes()
    run(home, "--refresh")
    assert (home / "pricing.yaml").read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["pricing.yaml"]


def test_a_source_without_a_url_is_an_error_row(home: Path, site: Site) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry("")})
    got = results(home)
    assert got["m-1"]["status"] == "error" and "no source page" in got["m-1"]["detail"]


def test_a_shared_url_is_fetched_once(home: Path, site: Site) -> None:
    url = site.page("/all", rows_html({"alpha-1": ("2.00", "0.20", "10.00"), "alpha-2": ("2.00", "0.20", "10.00"), "alpha-3": ("3.00", "0.30", "15.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url), "alpha-2": entry(url + "#other"), "alpha-3": entry(url, input=3.0, cached=0.3, output=15.0)})
    got = results(home)
    assert {m: r["status"] for m, r in got.items()} == {"alpha-1": "confirmed", "alpha-2": "confirmed", "alpha-3": "confirmed"}
    assert site.count("/all") == 1


def test_models_sharing_a_page_are_checked_against_their_own_rows(home: Path, site: Site) -> None:
    url = site.page("/pair", rows_html({"Model One": ("2.00", "0.20", "10.00"), "Model Two": ("4.00", "0.40", "20.00")}))
    write_table(home / "pricing.yaml", {"model-one": entry(url), "model-two": entry(url)})  # model-two is priced like model-one
    assert {m: r["status"] for m, r in results(home).items()} == {"model-one": "confirmed", "model-two": "changed"}


def test_redirects_are_followed_but_only_to_http_pages(home: Path, site: Site) -> None:
    site.page("/new", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    site.page("/old", "", status=301, Location="/new")
    site.page("/loop", "", status=302, Location="/loop")
    site.page("/local", "", status=302, Location="file:///etc/hostname")
    site.page("/ftp", "", status=302, Location="ftp://127.0.0.1/prices")
    site.page("/creds", "", status=302, Location="http://user:hunter2@127.0.0.1:9/prices")
    site.page("/broken", "", status=302, Location="http://[not-a-host/prices")
    write_table(
        home / "pricing.yaml",
        {
            "alpha-1": entry(site.base + "/old"),
            "loop-2": entry(site.base + "/loop"),
            "file-3": entry(site.base + "/local"),
            "ftp-4": entry(site.base + "/ftp"),
            "creds-5": entry(site.base + "/creds"),
            "broken-6": entry(site.base + "/broken"),
        },
    )
    got = results(home)
    assert got["alpha-1"]["status"] == "confirmed"
    assert got["loop-2"]["status"] == "error" and "HTTP 302" in got["loop-2"]["detail"]
    assert got["file-3"]["status"] == "error" and "not allowed" in got["file-3"]["detail"]
    assert got["ftp-4"]["status"] == "error" and "refused" in got["ftp-4"]["detail"]
    assert got["creds-5"]["status"] == "error" and "user name or password" in got["creds-5"]["detail"] and "hunter2" not in got["creds-5"]["detail"]
    assert got["broken-6"]["status"] == "error"


def test_the_reply_behind_an_http_error_is_closed(site: Site) -> None:
    """A 404 raises an HTTPError that is also the reply, and it holds its connection until it is closed (a ResourceWarning
    in Python's development mode when it is left to the garbage collector)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(pricefetch.FetchError, match="HTTP 404"):
            pricefetch.fetch(site.base + "/missing", allow_private_hosts=True)
        gc.collect()
    assert [str(w.message) for w in caught if issubclass(w.category, ResourceWarning) and "HTTPError" in str(w.message)] == []


def test_an_https_page_cannot_redirect_down_to_http() -> None:
    handler = pricefetch._SafeRedirects(allow_private_hosts=True)  # this test is about schemes; the hosts are tested below
    request = urllib.request.Request("https://example.com/prices")
    with pytest.raises(urllib.error.HTTPError, match="refused"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://example.com/prices")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://example.org/prices").full_url == "https://example.org/prices"


def test_an_oversized_page_is_an_error_and_is_not_read(home: Path, site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricefetch, "MAX_PAGE_BYTES", 8 * 1024)
    big = rows_html({"big-1": ("2.00", "0.20", "10.00")}) + "<!--" + "x" * 20_000 + "-->"
    write_table(home / "pricing.yaml", {"big-1": entry(site.page("/big", big))})
    got = results(home)
    assert got["big-1"]["status"] == "error" and "larger than 8 KB" in got["big-1"]["detail"]


def test_an_oversized_page_without_a_length_header_is_cut_off_too(home: Path, site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricefetch, "MAX_PAGE_BYTES", 8 * 1024)

    class Unsized(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"<p>" + b"x" * 30_000)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Unsized)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        write_table(home / "pricing.yaml", {"big-1": entry(f"http://127.0.0.1:{server.server_address[1]}/")})
        assert "larger than 8 KB" in results(home)["big-1"]["detail"]
    finally:
        server.shutdown()
        server.server_close()


def test_non_text_pages_and_non_http_sources_are_errors(home: Path, site: Site) -> None:
    site.page("/pdf", b"%PDF-1.4", content_type="application/pdf")
    write_table(home / "pricing.yaml", {"pdf-1": entry(site.base + "/pdf"), "ftp-2": entry("ftp://example.com/prices"), "file-3": entry("file:///etc/hostname")})
    got = results(home)
    assert "not a text page" in got["pdf-1"]["detail"]
    assert "only http and https" in got["ftp-2"]["detail"] and "only http and https" in got["file-3"]["detail"]


def test_a_refused_connection_is_an_error_and_exit_code_1(home: Path, site: Site) -> None:
    dead = site.base
    site.close()  # nothing listens on that port any more
    write_table(home / "pricing.yaml", {"m-1": entry(dead + "/p")})
    out = run(home, "--refresh")
    assert out.exit_code == 1
    assert "m-1" in out.stdout and "error" in out.stdout
    assert "failed:" in out.stderr and "127.0.0.1" in out.stderr


def test_exit_code_is_1_only_when_every_fetch_failed(home: Path, site: Site) -> None:
    good = site.page("/ok", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(good), "delta-4": entry(site.base + "/missing")})
    assert run(home, "--refresh").exit_code == 0  # one page failed, one worked
    write_table(home / "pricing.yaml", {"delta-4": entry(site.base + "/missing"), "delta-5": entry(site.base + "/gone")})
    assert run(home, "--refresh").exit_code == 1


def test_progress_names_the_host_and_never_the_query(home: Path, site: Site) -> None:
    url = site.page("/pricing", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url + "?token=SECRET-123&x=1")})
    out = run(home, "--refresh")
    assert "fetching 127.0.0.1/pricing ... ok" in out.stderr
    assert "SECRET" not in out.stderr + out.stdout
    assert "SECRET" not in run(home, "--refresh", "--json").stdout


def test_requests_carry_a_user_agent_and_no_cookies(home: Path, site: Site) -> None:
    site.page("/a", "", status=302, Location="/b", **{"Set-Cookie": "session=abc; Path=/"})
    site.page("/b", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}), **{"Set-Cookie": "other=1; Path=/"})
    write_table(home / "pricing.yaml", {"alpha-1": entry(site.base + "/a")})
    assert results(home)["alpha-1"]["status"] == "confirmed"
    headers = dict(site.hits)
    assert headers["/a"]["user-agent"].startswith("agentfare/") and headers["/b"]["user-agent"] == headers["/a"]["user-agent"]
    assert "cookie" not in headers["/a"] and "cookie" not in headers["/b"]  # the cookie set by /a does not follow the redirect
    assert headers["/a"]["accept-encoding"] == "identity"


def test_unverified_flag_also_limits_the_refresh(home: Path, site: Site) -> None:
    site.page("/naming", "<p>eta-7 is a reviewer model.</p>")
    write_table(home / "pricing.yaml", {"alpha-1": entry(site.base + "/alpha"), "eta-7": entry(site.base + "/naming", input="unverified")})
    assert list(results(home, "--unverified")) == ["eta-7"]
    assert site.count("/alpha") == 0 and site.count("/naming") == 1


def test_refresh_json_has_stable_keys(home: Path, site: Site) -> None:
    build_mixed(home, site)
    out = run(home, "--refresh", "--json")
    data = json.loads(out.stdout)
    assert list(data) == ["pricing", "pages", "results", "summary", "written", "warnings"]
    assert data["summary"] == {"confirmed": 3, "changed": 1, "not_found": 2, "error": 1}
    assert data["written"] is None
    assert list(data["results"][0]) == ["model", "status", "detail", "source_host", "prices_per_mtok", "nearby_per_mtok"]
    assert [p["path"] for p in data["pages"]] == sorted(p["path"] for p in data["pages"])
    assert next(p for p in data["pages"] if p["path"] == "/missing")["ok"] is False
    assert [w["model"] for w in data["warnings"]] == ["eta-7"]
    assert "fetching" in out.stderr  # progress goes to stderr, stdout stays pure JSON


# ---- --refresh --write ----------------------------------------------------------------------------------
def test_write_only_moves_the_checked_date_of_confirmed_models(home: Path, site: Site) -> None:
    build_mixed(home, site, aliases={"alpha-one": "alpha-1"})
    original = home.parent / "original.yaml"
    original.write_bytes((home / "pricing.yaml").read_bytes())
    out = run(home, "--refresh", "--write")
    assert out.exit_code == 0
    before, after = load_pricing(path=original), load_pricing(path=home / "pricing.yaml")
    confirmed = {"alpha-1", "eps-5", "zeta-6"}
    assert set(after.models) == set(before.models) and after.aliases == before.aliases
    for name, old in before.models.items():
        new = after.models[name]
        assert new.checked == (TODAY if name in confirmed else OLD), name
        assert replace(new, checked=old.checked) == old, f"{name}: only 'checked' may differ"
    assert f"'checked' set to today for 3 confirmed models in {home / 'pricing.yaml'}; no price was changed." in flat(out.stdout)
    written = (home / "pricing.yaml").read_text()
    assert "replaces the built-in table completely" in written and "# alpha-1 | source: " in written
    assert not (home / "pricing.yaml.tmp").exists()


def test_write_creates_the_override_from_the_builtin_table(home: Path, site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = write_table(
        tmp_path / "packaged.yaml",
        {"alpha-1": entry(site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")})), note="a note"), "beta-2": entry(site.base + "/missing")},
        aliases={"alpha-dated": "alpha-1"},
    )
    monkeypatch.setattr(pricing, "builtin_path", lambda: packaged)
    monkeypatch.setattr("agentfare.commands.prices.builtin_path", lambda: packaged)
    assert not (home / "pricing.yaml").exists()
    out = run(home, "--refresh", "--write")
    assert out.exit_code == 0
    made = load_pricing(path=home / "pricing.yaml")
    assert made.models["alpha-1"].checked == TODAY and made.models["alpha-1"].note == "a note"
    assert made.models["beta-2"].checked == OLD and made.aliases == {"alpha-dated": "alpha-1"}
    assert "now replaces the built-in table for every model" in flat(out.stdout)
    assert "override file" in run(home).stdout  # and the next run says it is the one in use


def test_a_failed_write_leaves_the_table_as_it_was_and_says_why(home: Path, site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)})
    before = (home / "pricing.yaml").read_bytes()

    def refuse(*args: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("agentfare.safeio.os.replace", refuse)
    out = run(home, "--refresh", "--write")
    assert out.exit_code == 2 and f"cannot write {home / 'pricing.yaml'}: Permission denied" in flat(out.stderr)
    assert (home / "pricing.yaml").read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["pricing.yaml"]  # no half-written temp file left behind


def test_write_is_reported_in_the_json(home: Path, site: Site) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)})
    data = json.loads(run(home, "--refresh", "--write", "--json").stdout)
    assert data["written"] == {"path": str(home / "pricing.yaml"), "checked_updated": ["alpha-1"], "created": False}
    assert load_pricing(path=home / "pricing.yaml").models["alpha-1"].checked == TODAY


def test_refreshing_nothing_is_fine(home: Path, site: Site) -> None:
    write_table(home / "pricing.yaml", {"alpha-1": entry(site.base + "/alpha")})
    text = run(home, "--refresh", "--unverified")
    assert text.exit_code == 0 and "no models to refresh." in text.stdout
    data = json.loads(run(home, "--refresh", "--unverified", "--json").stdout)
    assert data["results"] == [] and data["pages"] == [] and data["summary"] == {"confirmed": 0, "changed": 0, "not_found": 0, "error": 0}
    assert site.hits == []


def test_write_leaves_the_file_alone_when_no_date_would_move(home: Path, site: Site) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url, checked=TODAY), "delta-4": entry(site.base + "/missing")})
    before = (home / "pricing.yaml").read_bytes()
    out = run(home, "--refresh", "--write")
    assert "nothing written" in out.stdout
    assert (home / "pricing.yaml").read_bytes() == before


def test_write_without_confirmations_does_not_create_an_override(home: Path, site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = write_table(tmp_path / "packaged.yaml", {"delta-4": entry(site.base + "/missing")})
    monkeypatch.setattr(pricing, "builtin_path", lambda: packaged)
    monkeypatch.setattr("agentfare.commands.prices.builtin_path", lambda: packaged)
    out = run(home, "--refresh", "--write")
    assert out.exit_code == 1 and "nothing written" in out.stdout
    assert not (home / "pricing.yaml").exists()


# ---- reading pages: text extraction, prices, names ----------------------------------------------------------
def prices_of(text: str) -> list[str]:
    return [str(p.value) for p in pricefetch.find_prices(text)]


def test_text_extraction_drops_scripts_styles_and_comments_and_decodes_entities() -> None:
    html = (
        "<html><head><title>T</title><style>.x{}</style><script>var p='$1 / MTok'</script></head>"
        "<body><!-- $2 / MTok --><noscript>$3 / MTok</noscript><p>Fish&nbsp;&amp;&nbsp;chips &#36;4.00 / MTok</p>"
        "<svg><text>$5 / MTok</text></svg><p>a<b>b</b>c</p></body></html>"
    )
    assert pricefetch.extract_text(html) == "T\nFish & chips $4.00 / MTok\na b c"


def test_a_table_row_is_one_line_however_the_cells_are_built() -> None:
    html = (
        "<table><tr><td><div><a>Claude X 1</a><span>blurb</span></div></td><td><div>$2<!-- --> <span>/ MTok</span></div></td>"
        "<td>$10 / MTok</td></tr><tr><th>Name</th><td>&nbsp;</td><td>x</td></tr><td>loose</td><td>cells</td></table>"
    )
    assert pricefetch.extract_text(html).splitlines() == ["Claude X 1 blurb | $2 / MTok | $10 / MTok", "Name | | x", "loose | cells"]


def test_markdown_is_read_as_text_with_tables_links_and_emphasis_flattened() -> None:
    md = "## [Grok](/x) `grok-4.7`\n\n| Type | Price |\n| :--- | ---: |\n| **Input** | $2.00 / 1M tokens |\n"
    assert pricefetch.extract_text(md, "text/markdown") == "## Grok grok-4.7\n| Type | Price |\n| Input | $2.00 / 1M tokens |"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$2 / MTok", ["2"]),
        ("$0.002 per 1K tokens", ["2.000"]),
        ("$1,250.50 per 1M tokens", ["1250.50"]),
        ("US$3 per million tokens and 4.5 USD / 1M tokens", ["3", "4.5"]),
        ("$0.30 per 1,000,000 tokens", ["0.30"]),
        ("Prices per 1M tokens.\nInput | $3.50 |", ["3.50"]),
        ("Prices per 1K tokens.\nInput | $0.0035 |", ["3.5000"]),
        ("Prices per 1K tokens.\nsome text, then per 1M tokens:\n$7", ["7"]),
        ("$3.50", []),  # the page states no unit, so it may be a price per request
        ("$3.50 per request and $2 per image and $4 / 1,000 requests", []),
        ("$14 per 1,000 grounded prompts and $9 / 1,000 search queries", []),
        ("$1.00 / 1,000,000 tokens per hour (storage)", []),
        ("$1.00 / 1M tokens per month", []),
        ("A$5 per 1M tokens", []),  # another dollar
        ("$1,50 per 1M tokens", []),  # a decimal comma is not read as $1
        ("$1.5.3 per 1M tokens", []),
        ("Input | $2 | 1M tokens |", ["2"]),  # the unit in the next markdown cell
        ("Per 1M tokens\nInput | $2 | 200K prompt tokens | $4 | 5M tokens |", ["2", "4"]),  # other cells are not units
    ],
)
def test_a_price_counts_only_when_the_page_ties_it_to_a_token_unit(text: str, expected: list[str]) -> None:
    assert prices_of(text) == expected


def test_bare_numbers_count_only_in_a_table_cell_under_a_unit_statement() -> None:
    text = "Prices per 1M tokens.\n| m-1 | 2.00 | 0.20 | 10.00 |\nlimit 500 requests\n| Rate | 500 |"
    assert [(p.bare, str(p.value)) for p in pricefetch.find_prices(text)] == [(True, "2.00"), (True, "0.20"), (True, "10.00"), (True, "500")]
    table = parse_pricing({"models": {"m-1": {"input": 2, "cached_input": 0.2, "output": 10}}})
    assert pricefetch.check_page(text, table, ["m-1"])["m-1"].status == "confirmed"  # in the row of the model
    block = "Prices per 1M tokens.\nm-1\n\n| Rate | 2.00 |\n| Limit | 0.20 |"
    assert pricefetch.check_page(block, table, ["m-1"])["m-1"].status == "not_found"  # bare cells away from the name do not count


def test_names_match_ids_display_names_and_aliases_but_not_longer_names() -> None:
    table = parse_pricing(
        {
            "models": {
                "claude-sonnet-5": {"input": 2, "output": 10},
                "claude-sonnet-5-5": {"input": 3, "output": 15},
                "gemini-3.5-flash": {"input": 1, "output": 2},
                "gemini-3.5-flash-lite": {"input": 5, "output": 6},
                "gpt-6.1-sol": {"input": 7, "output": 8},
            },
            "aliases": {"gpt-sol-dated@1": "gpt-6.1-sol"},
        }
    )

    def named(text: str) -> list[str]:
        return [m.model for m in pricefetch._mentions(text, table)]

    assert named("Claude Sonnet 5.5 and claude-sonnet-5-5 and Claude Sonnet 5") == ["claude-sonnet-5-5", "claude-sonnet-5-5", "claude-sonnet-5"]
    assert named("Gemini 3.5 Flash-Lite and Gemini 3.5 Flash Lite and gemini-3.5-flash-lite.") == ["gemini-3.5-flash-lite"] * 3
    assert named("Gemini 3.5 Flash, gemini-3.5-flash") == ["gemini-3.5-flash"] * 2
    assert named("GPT-6.1 Sol / gpt-sol-dated@1 / openai/gpt-6.1-sol / xgpt-6.1-sol") == ["gpt-6.1-sol"] * 3
    assert named("claude-sonnet-5-20260101 claude-sonnet-5x") == []


def test_each_page_number_vouches_for_one_price_only() -> None:
    table = parse_pricing({"models": {"m-1": {"input": 2, "cached_input": 2, "output": 10}}})
    assert pricefetch.check_page("Per 1M tokens\nm-1 | $2 | $10", table, ["m-1"])["m-1"].status == "changed"
    assert pricefetch.check_page("Per 1M tokens\nm-1 | $2 | $2 | $10", table, ["m-1"])["m-1"].status == "confirmed"


def test_a_model_is_confirmed_against_its_own_row_not_a_neighbours() -> None:
    table = parse_pricing({"models": {"m-1": {"input": 1, "output": 5}, "m-2": {"input": 2, "output": 9}}})
    text = "Prices per 1M tokens.\n| m-1 | $1 | $5 |\n| m-2 | $2 | $10 |"
    got = pricefetch.check_page(text, table, ["m-1", "m-2"])
    assert got["m-1"].status == "confirmed" and got["m-2"].status == "changed"
    assert [(c.field, c.found) for c in got["m-2"].fields] == [("input", True), ("output", False)]
    assert got["m-2"].nearby == (Decimal("2"), Decimal("10"))


def test_a_cross_reference_in_prose_does_not_cut_a_model_page_short() -> None:
    table = parse_pricing({"models": {"m-1": {"input": 2, "output": 10}, "m-2": {"input": 1, "output": 5}}})
    text = "# M 1\nm-1 is the older one. See m-2 for the newer model.\nPer 1M tokens\nInput | $2\nOutput | $10\n"
    assert pricefetch.check_page(text, table, ["m-1"])["m-1"].status == "confirmed"


def test_a_page_full_of_unrelated_text_and_numbers_is_not_found() -> None:
    table = parse_pricing({"models": {"m-1": {"input": 2, "output": 10}}})
    assert pricefetch.check_page("nothing here $2 / MTok $10 / MTok", table, ["m-1"])["m-1"].status == "not_found"


def test_every_price_field_of_the_table_is_compared() -> None:
    row = "| m-1 | " + " | ".join(f"${i}" for i in range(1, len(PRICE_FIELDS) + 1)) + " |"
    table = parse_pricing({"models": {"m-1": {f: i for i, f in enumerate(PRICE_FIELDS, start=1)}}})
    got = pricefetch.check_page("Per 1M tokens\n" + row, table, ["m-1"])["m-1"]
    assert got.status == "confirmed" and [c.field for c in got.fields] == list(PRICE_FIELDS)
    lower = parse_pricing({"models": {"m-1": {f: i + (0.5 if f == PRICE_FIELDS[-1] else 0) for i, f in enumerate(PRICE_FIELDS, start=1)}}})
    changed = pricefetch.check_page("Per 1M tokens\n" + row, lower, ["m-1"])["m-1"]
    assert changed.status == "changed" and [c.field for c in changed.fields if not c.found] == [PRICE_FIELDS[-1]]


def test_the_row_a_test_page_uses_reads_like_the_builders_html() -> None:
    text = pricefetch.extract_text(rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    assert "alpha-1 The model | $2.00 | $0.20 | $10.00" in text.splitlines()
    assert "99.99" not in text and "77.77" not in text


# ---- a download that never ends, and sources that are not URLs ---------------------------------------------
class Raw:
    """A TCP server on 127.0.0.1 that hands each connection to ``script(conn)``, for replies http.server cannot give
    (trickling, endless, compressed by surprise). The request is read first and never looked at."""

    def __init__(self, script: Any) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.script = script
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            self.script(conn)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def raw() -> Iterator[Any]:
    servers: list[Raw] = []

    def start(script: Any) -> Raw:
        servers.append(Raw(script))
        return servers[-1]

    yield start
    for s in servers:
        s.close()


def trickle(head: bytes, body: bytes = b"x", every: float = 0.05, forever: int = 400) -> Any:
    """Send ``head`` then one ``body`` byte at a time, so no single read ever times out."""

    def script(conn: socket.socket) -> None:
        conn.sendall(head)
        for _ in range(forever):
            conn.sendall(body)
            time.sleep(every)

    return script


def fetch_or_hang(url: str, seconds: float = 8) -> tuple[Any, float]:
    """(page or the FetchError, seconds it took). A fetch that outlives ``seconds`` fails the test instead of hanging it."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["result"] = pricefetch.fetch(url, allow_private_hosts=True)
        except pricefetch.FetchError as exc:
            box["result"] = exc

    started = time.monotonic()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"the download was still running after {seconds} s: the deadline does not hold"
    return box["result"], time.monotonic() - started


def test_a_server_that_trickles_the_body_cannot_hold_the_download_past_the_deadline(raw: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricefetch, "DEADLINE_SECONDS", 1)
    server = raw(trickle(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1000000\r\n\r\n"))
    result, took = fetch_or_hang(server.base + "/")
    assert isinstance(result, pricefetch.FetchError) and "longer than 1 s" in str(result)
    assert took < 4


def test_a_server_that_trickles_the_headers_cannot_hold_the_download_past_the_deadline(raw: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricefetch, "DEADLINE_SECONDS", 1)
    server = raw(trickle(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nX-Slow: ", b"a"))
    result, took = fetch_or_hang(server.base + "/")
    assert isinstance(result, pricefetch.FetchError) and "longer than 1 s" in str(result)
    assert took < 4


def test_a_server_that_says_nothing_hits_the_deadline_too(raw: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pricefetch, "DEADLINE_SECONDS", 1)
    monkeypatch.setattr(pricefetch, "TIMEOUT_SECONDS", 30)
    server = raw(lambda conn: time.sleep(10))
    result, took = fetch_or_hang(server.base + "/")
    assert isinstance(result, pricefetch.FetchError) and "longer than 1 s" in str(result)
    assert took < 4


def test_a_compressed_reply_that_was_not_asked_for_is_an_error_not_garbage(raw: Any) -> None:
    body = gzip.compress(rows_html({"alpha-1": ("2.00", "0.20", "10.00")}).encode())
    head = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    server = raw(lambda conn: conn.sendall(head + body))
    result, _ = fetch_or_hang(server.base + "/")
    assert isinstance(result, pricefetch.FetchError) and "gzip" in str(result)


def test_a_page_cut_short_is_an_error_not_a_smaller_page(raw: Any) -> None:
    body = rows_html({"alpha-1": ("2.00", "0.20", "10.00")}).encode()
    head = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body) + 500}\r\n\r\n".encode()
    server = raw(lambda conn: conn.sendall(head + body))  # the connection closes 500 bytes early
    result, _ = fetch_or_hang(server.base + "/")
    assert isinstance(result, pricefetch.FetchError) and f"after {len(body)} of {len(body) + 500} bytes" in str(result)


def test_a_chunked_page_cut_short_is_an_error_too(raw: Any) -> None:
    head = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nTransfer-Encoding: chunked\r\n\r\n"
    server = raw(lambda conn: conn.sendall(head + b"40\r\n<p>only part of the promised chunk"))
    result, _ = fetch_or_hang(server.base + "/")
    assert isinstance(result, pricefetch.FetchError)


BAD_SOURCES = [
    "http://[bad/pricing",  # urlsplit itself refuses it
    "http://127.0.0.1:9/préços",  # not ASCII: http.client cannot send it
    "http://a..b/p",  # an empty host label
    "http://127.0.0.1:99999999/p",  # port out of range
    "http://user:hunter2@127.0.0.1:9/p",  # credentials in a URL are never sent, so never tried
]


@pytest.mark.parametrize("source", BAD_SOURCES)
def test_a_source_that_is_not_a_fetchable_url_is_an_error_row_not_a_crash(home: Path, source: str) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(source), "m-2": entry("http://127.0.0.1:9/p")})
    out = run(home, "--refresh")
    assert out.exception is None or isinstance(out.exception, SystemExit), out.output
    assert next(ln for ln in out.stdout.splitlines() if ln.startswith("m-1")).split()[1] == "error"
    assert "hunter2" not in out.stdout + out.stderr
    assert json.loads(run(home, "--refresh", "--json").stdout)["results"][0]["status"] == "error"


@pytest.mark.parametrize("source", BAD_SOURCES)
def test_the_offline_table_lists_a_model_whose_source_is_not_a_url(home: Path, source: str) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(source)})
    out = run(home)
    assert out.exit_code == 0 and out.exception is None, out.output
    assert "m-1" in out.stdout and "hunter2" not in out.stdout
    assert json.loads(run(home, "--json").stdout)["models"][0]["model"] == "m-1"


def test_credentials_in_a_source_url_are_refused_with_a_clear_reason(home: Path) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry("http://user:hunter2@127.0.0.1:9/p")})
    detail = results(home)["m-1"]["detail"]
    assert "user name or password" in detail and "hunter2" not in detail


def test_when_no_model_could_be_checked_the_exit_code_is_1(home: Path) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(""), "m-2": entry("")})
    out = run(home, "--refresh")
    assert out.exit_code == 1 and out.stdout.count("no source page") == 2


# ---- which number is which: labels, tiers, and numbers that only look right ---------------------------------
def status_of(text: str, model: str = "alpha-1", **prices: Any) -> pricefetch.ModelCheck:
    table = parse_pricing({"models": {model: prices}})
    return pricefetch.check_page(text, table, [model])[model]


def found_fields(check: pricefetch.ModelCheck) -> dict[str, tuple[bool, Decimal | None]]:
    return {c.field: (c.found, c.page) for c in check.fields}


HEADER_ROW = (  # the shape of a pricing table with one row per model and several prices per row
    "Model | Base tokens | Prompt caching\n"
    "Name | Input | Output | 5m writes | 1h writes | Hits and refreshes\n"
    "alpha-1 A short blurb | $2 / MTok | $10 / MTok | $2.50 / MTok | $4 / MTok | $0.20 / MTok\n"
)


def test_a_price_must_sit_under_its_own_column_not_under_another_columns_number() -> None:
    right = status_of(HEADER_ROW, input=2, output=10, cache_write=2.5, cache_write_1h=4, cached_input=0.2)
    assert right.status == "confirmed" and all(found for found, _ in found_fields(right).values())
    wrong = status_of(HEADER_ROW, input=4, output=10, cache_write=2.5, cache_write_1h=4, cached_input=0.2)  # 4 is the 1h write
    assert wrong.status == "changed"
    assert found_fields(wrong)["input"] == (False, Decimal("2")) and found_fields(wrong)["cache_write_1h"] == (True, Decimal("4"))


def test_a_column_is_found_by_its_header_even_when_the_order_is_not_the_tables() -> None:
    swapped = status_of(HEADER_ROW, input=10, output=2)  # the page's input is 2 and its output 10
    assert swapped.status == "changed" and found_fields(swapped) == {"input": (False, Decimal("2")), "output": (False, Decimal("10"))}


TIERS = (  # the shape of a page that repeats the whole table for each service tier, short and long context side by side
    "Prices per 1M tokens.\n"
    "Standard\n| Short context | Long context\n"
    "Model | Input | Cached input | Cache writes | Output | Input | Cached input | Cache writes | Output\n"
    "alpha-1 | $10.00 | $1.00 | $12.50 | $50.00 | $20.00 | $2.00 | $25.00 | $75.00\n"
    "Batch\n| Short context | Long context\n"
    "Model | Input | Cached input | Cache writes | Output | Input | Cached input | Cache writes | Output\n"
    "alpha-1 | $5.00 | $0.50 | $6.25 | $25.00 | $10.00 | $1.00 | $12.50 | $37.50\n"
    "Fast\n| Short context | Long context\n"
    "Model | Input | Cached input | Cache writes | Output | Input | Cached input | Cache writes | Output\n"
    "alpha-1 | $20.00 | $2.00 | $25.00 | $100.00 | $40.00 | $4.00 | $50.00 | $150.00\n"
)


@pytest.mark.parametrize(
    ("factor", "expected"),
    [(1, "confirmed"), (2, "changed"), (Decimal("0.5"), "changed")],  # x2 is the Fast row, x0.5 the Batch row: not the standard price
)
def test_only_the_first_table_of_a_model_counts_not_its_other_tiers(factor: Any, expected: str) -> None:
    base = {"input": 10, "cached_input": 1, "cache_write": 12.5, "output": 50}
    got = status_of(TIERS, **{f: Decimal(str(v)) * factor for f, v in base.items()})
    assert got.status == expected


def test_a_price_that_is_only_the_long_context_number_is_not_the_standard_price() -> None:
    assert status_of(TIERS, input=20, cached_input=1, cache_write=12.5, output=50).status == "changed"
    assert status_of(TIERS, input=10, cached_input=1, cache_write=12.5, output=75).status == "changed"


def label_rows(alpha_caching: bool = True) -> str:
    """One section per model with a labelled row per price and a table per tier, the way a per-model page is laid out."""
    caching = "Context caching price | Free of charge | $0.15 $1.00 / 1,000,000 tokens per hour (storage price)\n" if alpha_caching else ""
    return (
        "Alpha One\nalpha-1\nStandard\n| Free Tier | Paid Tier, per 1M tokens in USD\n"
        "Input price | Free of charge | $1.50\n"
        "Output price (including thinking tokens) | Free of charge | $9.00\n"
        f"{caching}"
        "Grounding | Not available | 5,000 free requests per month, then $14 per 1,000 requests.\n"
        "Batch\n| Free Tier | Paid Tier, per 1M tokens in USD\n"
        "Input price | Not available | $0.75\n"
        "Output price (including thinking tokens) | Not available | $4.50\n"
        "Context caching price | Not available | $0.075 $1.00 / 1,000,000 tokens per hour (storage price)\n"
        "Beta Two\nbeta-2\nStandard\n| Free Tier | Paid Tier, per 1M tokens in USD\n"
        "Input price | Free of charge | $0.30\n"
        "Output price (including thinking tokens) | Free of charge | $2.50\n"
        "Context caching price | Free of charge | $0.03\n"
    )


def test_a_labelled_row_is_read_by_its_label_and_only_in_the_first_tier() -> None:
    assert status_of(label_rows(), input=1.5, cached_input=0.15, output=9).status == "confirmed"  # the storage fee is not a price
    batch = status_of(label_rows(), input=0.75, cached_input=0.075, output=4.5)  # the Batch numbers, all present on the page
    assert batch.status == "changed" and found_fields(batch)["input"] == (False, Decimal("1.50"))


def test_a_model_without_a_row_does_not_borrow_the_same_row_of_the_next_model() -> None:
    table = parse_pricing({"models": {"alpha-1": {"input": 1.5, "cached_input": 0.03, "output": 9}, "beta-2": {"input": 0.3, "cached_input": 0.03, "output": 2.5}}})
    got = pricefetch.check_page(label_rows(alpha_caching=False), table, ["alpha-1", "beta-2"])
    assert got["beta-2"].status == "confirmed"
    assert got["alpha-1"].status == "changed" and found_fields(got["alpha-1"])["cached_input"] == (False, None)  # 0.03 is Beta's caching price


def test_the_second_price_on_a_row_is_a_long_prompt_rate_not_the_price() -> None:
    row = "Per 1M tokens\nalpha-1\nInput price | Free of charge | $1.25, prompts <= 200k tokens $2.50, prompts > 200k tokens\nOutput price | Free of charge | $10\n"
    assert status_of(row, input=1.25, output=10).status == "confirmed"
    assert status_of(row, input=2.5, output=10).status == "changed"


def test_a_price_on_a_line_of_its_own_is_labelled_by_the_line_above_it() -> None:
    page = "alpha-1\nPricing\nText tokens\nPer 1M tokens\nInput\n$0.20\nCached input\n$0.02\nOutput\n$1.20\nQuick comparison\nInput\nCached input\nOutput\nother-9\n$2.00\nalpha-1\n$0.20\n"
    assert status_of(page, input=0.2, cached_input=0.02, output=1.2).status == "confirmed"
    assert status_of(page, input=2, cached_input=0.02, output=1.2).status == "changed"  # 2.00 is the neighbour's price in the comparison
    xai = "alpha-1\nPricing\nInput\nTokens\n$2.00 / 1M tokens\nCached tokens\n$0.50 / 1M tokens\nOutput\nTokens\n$6.00 / 1M tokens\n"
    assert status_of(xai, input=2, cached_input=0.5, output=6).status == "confirmed"
    assert status_of(xai, input=6, cached_input=0.5, output=2).status == "changed"


def test_labels_that_name_a_tier_or_two_prices_are_not_taken_for_a_price() -> None:
    page = "alpha-1\n| Input, long context | Input | Output |\n| $4 / MTok | $2 / MTok | $10 / MTok |\nPer 1M tokens\nInput and output | $99 / MTok\n"
    assert status_of(page, input=2, output=10).status == "confirmed"
    assert status_of(page, input=4, output=10).status == "changed"


def test_without_labels_the_numbers_must_be_exactly_the_tables_prices() -> None:
    prose = "alpha-1 costs $2 per million input tokens and $10 per million output tokens."
    assert status_of(prose, input=2, output=10).status == "confirmed"
    surplus = "alpha-1: $2 / MTok, $4 / MTok, $10 / MTok"
    got = status_of(surplus, input=4, output=10)  # both numbers are there, but so is a third and nothing says which is which
    assert got.status == "changed" and "cannot be told apart" in got.detail
    assert all(found for found, _ in found_fields(got).values())


def test_a_summary_that_lacks_a_price_does_not_hide_the_full_table_after_it() -> None:
    page = "alpha-1\nInput | $2 / MTok\nOutput | $10 / MTok\n\nalpha-1 in detail\n| Input | Cached input | Output |\n| $2 / MTok | $0.20 / MTok | $10 / MTok |\n"
    assert status_of(page, input=2, cached_input=0.2, output=10).status == "confirmed"


def test_a_changed_row_reports_the_number_the_page_shows_for_the_price(home: Path, site: Site) -> None:
    url = site.page("/p", rows_html({"alpha-1": ("2.50", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)})
    got = results(home)["alpha-1"]
    assert got["status"] == "changed"
    assert got["prices_per_mtok"]["input"]["page_price"] == {"usd": "2.50", "cents": 250, "ucents": 250_000_000}
    assert got["prices_per_mtok"]["output"]["page_price"]["ucents"] == 1_000_000_000
    assert "input 2.00 (page shows 2.50)" in flat(run(home, "--refresh").stdout)


# ---- a page built to be slow to read ---------------------------------------------------------------------------
# Each input is sized so that code which rescans the line for every match needs a minute or more; reading it once
# takes well under a second, so the limit leaves room for a loaded machine.
def seconds(work: Any) -> float:
    started = time.monotonic()
    work()
    return time.monotonic() - started


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        pytest.param("-" * 200_000 + "x", "text/markdown", id="dashes"),  # a line of dashes that is not a table rule
        pytest.param("[" * 200_000, "text/markdown", id="open-brackets"),  # link text that never closes
        pytest.param("[a" * 100_000 + "]", "text/plain", id="one-close"),
    ],
)
def test_reading_markdown_stays_linear_on_hostile_lines(body: str, kind: str) -> None:
    assert seconds(lambda: pricefetch.extract_text(body, kind)) < 3


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Per 1M tokens\nm-1 " + "| $1 / MTok " * 50_000, id="one-row-endless-prices"),
        pytest.param("m-1 $2 / MTok $10 / MTok " * 30_000, id="name-repeated-on-one-line"),
        pytest.param("m-1 " * 300_000, id="name-a-hundred-thousand-times"),
        pytest.param("Per 1M tokens\n" + "m-1 | $2 | $10\n" * 40_000, id="a-row-per-line"),
    ],
)
def test_checking_a_page_stays_linear_however_it_is_built(text: str) -> None:
    table = parse_pricing({"models": {"m-1": {"input": 2, "output": 10}}})
    assert seconds(lambda: pricefetch.check_page(text, table, ["m-1"])) < 3


# ---- --write and the file it rewrites -------------------------------------------------------------------------
def test_write_follows_a_symlinked_override_instead_of_replacing_the_link(home: Path, site: Site, tmp_path: Path) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    kept = write_table(tmp_path / "dotfiles.yaml", {"alpha-1": entry(url)})  # the real file, kept elsewhere
    (home / "pricing.yaml").symlink_to(kept)
    assert run(home, "--refresh", "--write").exit_code == 0
    assert (home / "pricing.yaml").is_symlink() and (home / "pricing.yaml").resolve() == kept.resolve()
    assert load_pricing(path=kept).models["alpha-1"].checked == TODAY
    assert not list(tmp_path.glob("*.tmp")) and not list(home.glob("*.tmp"))


def test_write_keeps_the_permissions_of_the_override(home: Path, site: Site) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)}).chmod(0o600)
    assert run(home, "--refresh", "--write").exit_code == 0
    assert (home / "pricing.yaml").stat().st_mode & 0o777 == 0o600


def test_the_help_says_what_write_does_to_the_file_it_rewrites() -> None:
    from agentfare.commands.prices import HELP

    assert "comments" in HELP and "first table" in HELP


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Input", "input"), ("Base Input Tokens", "input"), ("Input price", "input"), ("Uncached input", "input"),
        ("Input (cache miss)", "input"), ("Output", "output"), ("Output price (including thinking tokens)", "output"),
        ("Cached input", "cached_input"), ("Cache reads", "cached_input"), ("Cache hits & refreshes", "cached_input"),
        ("Hits and refreshes", "cached_input"), ("Context caching price", "cached_input"), ("Cached tokens", "cached_input"),
        ("Cache writes", "cache_write"), ("5m writes", "cache_write"), ("Cache write (5-minute)", "cache_write"),
        ("1h writes", "cache_write_1h"), ("1-hour cache writes", "cache_write_1h"), ("Cache write (1 hour)", "cache_write_1h"),
        ("Input and output", None), ("Model", None), ("Tokens", None), ("Free Tier", None),
        ("Input (long context)", None), ("Batch input", None), ("Fast output", None), ("Input, prompts > 200k tokens", None),
    ],
)  # fmt: skip
def test_a_label_names_the_price_it_is_the_column_or_row_of(label: str, expected: str | None) -> None:
    assert pricefetch.field_of(label) == expected


def test_words_just_before_a_price_label_it() -> None:
    page = "Per 1M tokens\nalpha-1\nInput: $2.00 / MTok\nOutput: $10.00 / MTok\n"
    assert status_of(page, input=2, output=10).status == "confirmed"
    swapped = status_of(page, input=10, output=2)  # the same two numbers, but each under the wrong label
    assert swapped.status == "changed" and found_fields(swapped)["input"] == (False, Decimal("2.00"))


def test_a_model_named_with_no_price_of_its_own_does_not_take_the_next_models() -> None:
    table = parse_pricing({"models": {"alpha-1": {"input": 2, "output": 10}, "beta-2": {"input": 2, "output": 10}}})
    page = "Per 1M tokens\nalpha-1\nA model with no price list.\nbeta-2\nInput | $2 / MTok\nOutput | $10 / MTok\n"
    got = pricefetch.check_page(page, table, ["alpha-1", "beta-2"])
    assert (got["alpha-1"].status, got["beta-2"].status) == ("not_found", "confirmed")


# ---- bytes that are not clean UTF-8 -------------------------------------------------------------------------------
def test_pages_in_other_encodings_or_with_broken_bytes_are_still_read(home: Path, site: Site) -> None:
    def row(name: str) -> str:
        return rows_html({name: ("2.00", "0.20", "10.00")})

    pages = {
        "latin-1": site.page("/latin", ("<p>Pr\u00e9cios \u2013 caf\u00e9</p>" + row("latin-1")).encode("cp1252"), content_type="text/html; charset=windows-1252"),
        "broken-2": site.page("/broken", b"<p>\xff\xfe\x80 not utf-8 \xc3</p>" + row("broken-2").encode(), content_type="text/html; charset=utf-8"),
        "utf16-3": site.page("/utf16", row("utf16-3").encode("utf-16"), content_type="text/html; charset=utf-16"),
        "unknown-4": site.page("/unknown", row("unknown-4").encode(), content_type="text/html; charset=klingon"),  # a charset Python has never heard of
    }
    write_table(home / "pricing.yaml", {name: entry(url) for name, url in pages.items()})
    assert {m: r["status"] for m, r in results(home).items()} == {name: "confirmed" for name in pages}


def test_numbers_in_attributes_hidden_elements_and_alt_text_are_not_prices(home: Path, site: Site) -> None:
    html = (
        "<html><head><meta name='description' content='alpha-1 $99 / MTok'></head><body>"
        "<table><tr><th>Model</th><th>Input</th><th>Output</th></tr>"
        "<tr><td data-price='$99 / MTok' title='alpha-1 $77 / MTok'>ALPHA-1 <img alt='$55 / MTok' src='x.png'></td>"
        "<td aria-label='$44 / MTok'>$2 / MTok</td><td><span hidden-price='$33'>$10 / MTok</span></td></tr></table></body></html>"
    )
    write_table(home / "pricing.yaml", {"alpha-1": entry(site.page("/attrs", html), cached=None)})
    got = results(home)["alpha-1"]
    assert got["status"] == "confirmed" and [n["usd"] for n in got["nearby_per_mtok"]] == ["2.00", "10.00"]


# ---- what --write creates: private, and never through a link somebody planted --------------------------------------
@pytest.fixture
def umask022() -> Iterator[None]:
    old = os.umask(0o022)  # the usual default: a plain open() would make a world-readable file
    yield
    os.umask(old)


def test_a_new_override_is_private(home: Path, site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask022: None) -> None:
    packaged = write_table(tmp_path / "packaged.yaml", {"alpha-1": entry(site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")})))})
    monkeypatch.setattr(pricing, "builtin_path", lambda: packaged)
    monkeypatch.setattr("agentfare.commands.prices.builtin_path", lambda: packaged)
    assert run(home, "--refresh", "--write").exit_code == 0
    assert (home / "pricing.yaml").stat().st_mode & 0o777 == 0o600


def test_a_new_override_in_a_new_data_directory_makes_the_directory_private_too(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask022: None) -> None:
    packaged = write_table(tmp_path / "packaged.yaml", {"alpha-1": entry(site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")})))})
    monkeypatch.setattr(pricing, "builtin_path", lambda: packaged)
    monkeypatch.setattr("agentfare.commands.prices.builtin_path", lambda: packaged)
    fresh = tmp_path / "fresh"
    assert run(fresh, "--refresh", "--write").exit_code == 0
    assert (fresh.stat().st_mode & 0o777, (fresh / "pricing.yaml").stat().st_mode & 0o777) == (0o700, 0o600)


def test_an_existing_override_keeps_a_mode_the_user_chose(home: Path, site: Site, umask022: None) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)}).chmod(0o644)
    assert run(home, "--refresh", "--write").exit_code == 0
    assert (home / "pricing.yaml").stat().st_mode & 0o777 == 0o644


def test_write_does_not_follow_a_symlink_planted_at_the_old_temp_name(home: Path, site: Site, tmp_path: Path) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)})
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    (home / "pricing.yaml.tmp").symlink_to(victim)
    assert run(home, "--refresh", "--write").exit_code == 0
    assert victim.read_text() == "precious"
    assert load_pricing(path=home / "pricing.yaml").models["alpha-1"].checked == TODAY
    assert (home / "pricing.yaml.tmp").is_symlink()  # left as it was: nobody writes through it, nobody removes it


def test_write_does_not_follow_a_dangling_symlink_planted_at_the_old_temp_name(home: Path, site: Site, tmp_path: Path) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)})
    (home / "pricing.yaml.tmp").symlink_to(tmp_path / "created-by-the-attacker")
    assert run(home, "--refresh", "--write").exit_code == 0
    assert not (tmp_path / "created-by-the-attacker").exists()


def test_a_symlinked_override_keeps_the_mode_of_the_file_it_points_to(home: Path, site: Site, tmp_path: Path, umask022: None) -> None:
    url = site.page("/alpha", rows_html({"alpha-1": ("2.00", "0.20", "10.00")}))
    kept = write_table(tmp_path / "dotfiles.yaml", {"alpha-1": entry(url)})
    kept.chmod(0o640)
    (home / "pricing.yaml").symlink_to(kept)
    assert run(home, "--refresh", "--write").exit_code == 0
    assert (home / "pricing.yaml").is_symlink() and kept.stat().st_mode & 0o777 == 0o640


# ---- a server's words and a model's source are data: nothing in them reaches the terminal as a control character -----
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")  # all but newline and tab


def test_a_hostile_server_reply_never_reaches_the_terminal_as_a_control_character(home: Path, raw: Any) -> None:
    status = raw(lambda conn: conn.sendall(b"HTTP/1.1 503 \x1b]0;pwned\x07 \x1b[31mdown\x9b\xe2\x80\xae\r\nContent-Length: 0\r\n\r\n"))
    ctype = raw(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/\x1b[2J\x07x\r\nContent-Length: 2\r\n\r\nhi"))
    encoding = raw(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: \x1b[1mbr\x9b\r\nContent-Length: 2\r\n\r\nhi"))
    write_table(
        home / "pricing.yaml",
        {"m-status": entry(status.base + "/a"), "m-type": entry(ctype.base + "/b"), "m-coding": entry(encoding.base + "/c"), "m-path": entry(status.base + "/\x1b[2Jp")},
    )
    for args in ((), ("--json",)):
        out = run(home, "--refresh", *args)
        assert out.exit_code == 1, out.output + out.stderr  # every download failed
        assert not UNSAFE.search(out.stdout) and not UNSAFE.search(out.stderr), (args, out.stdout, out.stderr)
        assert "503" in out.stdout and "down" in out.stdout  # the reason is still there, just harmless
    assert "?" in run(home, "--refresh").stdout


def test_a_newline_in_a_server_reply_cannot_forge_a_line_of_the_results(home: Path, raw: Any) -> None:
    server = raw(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/x\rforged-2 confirmed done\r\nContent-Length: 2\r\n\r\nhi"))
    write_table(home / "pricing.yaml", {"real-1": entry(server.base + "/a")})
    out = run(home, "--refresh")
    assert not [ln for ln in (out.stdout + out.stderr).splitlines() if ln.startswith("forged-2")]
    assert not UNSAFE.search(out.stdout) and not UNSAFE.search(out.stderr)


def test_clean_text_is_the_one_in_render() -> None:
    from agentfare import render, textsafe

    assert render.clean_text is textsafe.clean_text
    assert render.clean_text("a\x1b]0;x\x07b\nc\x9bd‮e") == "a?]0;x?b?c?d?e"


# ---- a refresh does not go to this machine or its network, unless it is told ---------------------------------------
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


def resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    """Make every name resolve to ``addresses``, so a test needs no DNS."""
    answer = [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 80)) for a in addresses]
    monkeypatch.setattr(pricefetch.socket, "getaddrinfo", lambda *args, **kw: answer)


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True), ("8.8.8.8", True), ("2606:4700:4700::1111", True),
        ("127.0.0.1", False), ("127.255.255.254", False), ("::1", False), ("0.0.0.0", False), ("::", False),
        ("10.0.0.1", False), ("172.16.5.4", False), ("172.31.255.255", False), ("192.168.0.1", False),
        ("169.254.169.254", False), ("fe80::1", False), ("fe80::1%eth0", False), ("fc00::1", False), ("fd12:3456::1", False),
        ("100.64.0.1", False), ("224.0.0.1", False), ("255.255.255.255", False),
        ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False), ("::ffff:93.184.216.34", True),  # an IPv4 address written as IPv6
        ("2002:7f00:1::1", False), ("64:ff9b::7f00:1", False),  # 127.0.0.1 through 6to4 and through NAT64
        ("not-an-address", False),
    ],
)
def test_which_addresses_are_public(address: str, public: bool) -> None:
    assert pricefetch._is_public(address) is public


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "[::1]", "0.0.0.0", "[::]", "10.1.2.3", "172.16.0.9", "192.168.1.5", "169.254.169.254", "[fe80::1]", "[::ffff:127.0.0.1]"],
)
def test_a_source_on_this_machine_or_its_network_is_refused_and_never_contacted(home: Path, site: Site, host: str) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(f"http://{host}:{site.server.server_address[1]}/prices")})
    out = run(home, "--refresh", "--json", private=False)
    [row] = json.loads(out.stdout)["results"]
    assert out.exit_code == 1 and row["status"] == "error"
    assert "refusing to fetch" in row["detail"] and "--allow-private-hosts" in row["detail"]
    assert site.hits == []  # not a single request reached the local server


def test_localhost_is_refused_by_name(home: Path, site: Site) -> None:
    try:
        socket.getaddrinfo("localhost", 80)
    except OSError:
        pytest.skip("this machine does not resolve localhost")
    write_table(home / "pricing.yaml", {"m-1": entry(f"http://localhost:{site.server.server_address[1]}/prices")})
    [row] = json.loads(run(home, "--refresh", "--json", private=False).stdout)["results"]
    assert row["status"] == "error" and "refusing to fetch" in row["detail"] and site.hits == []


def test_the_refusal_is_in_the_text_output_too(home: Path, site: Site) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(site.page("/p", rows_html({"m-1": ("2.00", "0.20", "10.00")})))})
    out = run(home, "--refresh", private=False)
    assert out.exit_code == 1 and "failed: refusing to fetch 127.0.0.1" in flat(out.stderr) and "--allow-private-hosts" in flat(out.stdout + out.stderr)
    assert site.hits == []


def test_the_flag_lets_a_local_source_through(home: Path, site: Site) -> None:
    write_table(home / "pricing.yaml", {"m-1": entry(site.page("/p", rows_html({"m-1": ("2.00", "0.20", "10.00")})))})
    out = run(home, "--refresh", "--json")  # the helper adds --allow-private-hosts
    assert out.exit_code == 0 and json.loads(out.stdout)["results"][0]["status"] == "confirmed" and site.count("/p") == 1


def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "10.0.0.7")
    with pytest.raises(pricefetch.FetchError, match=r"refusing to fetch prices\.example\.com \(10\.0\.0\.7\).*--allow-private-hosts"):
        pricefetch.fetch("https://prices.example.com/p")


def test_one_private_answer_among_public_ones_is_enough_to_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "93.184.216.34", "127.0.0.1")
    with pytest.raises(pricefetch.FetchError, match="refusing to fetch"):
        pricefetch.fetch("http://rebind.example.com/p")


def test_a_name_that_does_not_resolve_is_an_error_with_the_resolvers_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kw: Any) -> None:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(pricefetch.socket, "getaddrinfo", fail)
    with pytest.raises(pricefetch.FetchError, match="Name or service not known"):
        pricefetch.fetch("http://nowhere.example.invalid/p")


def test_a_redirect_to_a_private_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = pricefetch._SafeRedirects()
    request = urllib.request.Request("http://prices.example.com/p")  # http, so that the scheme rule is not what refuses the https targets
    for target in ("http://127.0.0.1:9/x", "https://10.0.0.5/x", "https://[::1]/x", "https://169.254.169.254/latest/meta-data"):
        with pytest.raises(pricefetch.FetchError, match="refusing to fetch"):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    resolves_to(monkeypatch, "93.184.216.34")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://other.example.org/p").full_url == "https://other.example.org/p"
    allowed = pricefetch._SafeRedirects(allow_private_hosts=True)
    assert allowed.redirect_request(request, None, 302, "Found", {}, "https://10.0.0.5/x").full_url == "https://10.0.0.5/x"


def test_a_public_page_cannot_send_the_refresh_to_a_local_address(home: Path, site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first hop passes (this test calls 127.0.0.1 public, to have a server to start from); the redirect to 127.0.0.2 does not."""
    monkeypatch.setattr(pricefetch, "_is_public", lambda address: address == "127.0.0.1")
    site.page("/old", "", status=302, Location=f"http://127.0.0.2:{site.server.server_address[1]}/new")
    site.page("/new", rows_html({"m-1": ("2.00", "0.20", "10.00")}))
    write_table(home / "pricing.yaml", {"m-1": entry(site.base + "/old")})
    [row] = json.loads(run(home, "--refresh", "--json", private=False).stdout)["results"]
    assert row["status"] == "error" and "refusing to fetch 127.0.0.2" in row["detail"]
    assert site.count("/old") == 1 and site.count("/new") == 0


def test_the_flag_needs_refresh(home: Path) -> None:
    out = run(home, "--allow-private-hosts")
    assert out.exit_code == 2 and "--allow-private-hosts needs --refresh" in out.stderr


def test_the_help_says_private_hosts_are_refused_and_how_to_allow_them() -> None:
    from agentfare.commands.prices import HELP

    flat_help = flat(HELP)
    assert "--allow-private-hosts" in flat_help and "loopback" in flat_help and "link-local" in flat_help and "redirect" in flat_help
    out = CliRunner().invoke(app, ["prices", "--help"], env={"COLUMNS": "100"})
    assert "--allow-private-hosts" in out.stdout and "refused by default" in flat(out.stdout)


def test_every_source_in_the_packaged_table_is_a_public_host() -> None:
    """The packaged table needs no ``--allow-private-hosts``: every source is an http(s) URL on a name, or on a public address."""
    for model, price in load_pricing().models.items():
        parts = pricefetch.urllib.parse.urlsplit(price.source)
        host = parts.hostname or ""
        assert parts.scheme in ("http", "https") and host, model
        assert host != "localhost" and not host.endswith((".localhost", ".local", ".internal")), f"{model}: {host}"
        try:
            pricefetch.ipaddress.ip_address(host)
        except ValueError:
            continue  # a name: it is resolved when the page is fetched
        assert pricefetch._is_public(host), f"{model}: {host}"


# ---- a page that is one huge number ------------------------------------------------------------------------------
HUGE_DIGITS = 5 * 1024 * 1024 - 4096


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param("$" + "9" * HUGE_DIGITS, id="dollar-digits"),
        pytest.param("$" + "9" * (HUGE_DIGITS // 2) + "." + "9" * (HUGE_DIGITS // 2), id="digits-point-digits"),
        pytest.param("9" * HUGE_DIGITS + " USD", id="digits-usd"),
    ],
)
def test_a_five_megabyte_number_is_ignored_quickly_and_never_echoed(home: Path, site: Site, cell: str) -> None:
    url = site.page("/huge", f"<p>Prices per 1M tokens.</p><table><tr><td>alpha-1</td><td>{cell} / MTok</td><td>$10.00</td></tr></table>")
    write_table(home / "pricing.yaml", {"alpha-1": entry(url)})
    for args in ((), ("--json",)):
        started = time.monotonic()
        out = run(home, "--refresh", *args)
        assert time.monotonic() - started < 3
        assert out.exit_code == 0 and "9999999999" not in out.stdout + out.stderr
        assert len(out.stdout) < 10_000


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Per 1M tokens\nm-1 | $1234567890123 | $10", id="thirteen-digits"),
        pytest.param("Per 1M tokens\nm-1 | $1.234567890123 | $10", id="thirteen-significant-digits"),
        pytest.param("Per 1M tokens\nm-1 | $0.1234567890123 | $10", id="thirteen-decimals"),
        pytest.param("Per 1M tokens\nm-1 | $1,000,000.01 | $10", id="above-the-price-limit"),
        pytest.param("m-1 $2000.01 per 1K tokens, $10 per 1M tokens", id="per-thousand-above-the-limit"),
    ],
)
def test_a_number_a_price_could_not_be_is_not_read(text: str) -> None:
    assert [p.value for p in pricefetch.find_prices(text)] in ([Decimal(10)], [])


def test_numbers_up_to_twelve_digits_and_the_limit_are_still_prices() -> None:
    found = [p.value for p in pricefetch.find_prices("Per 1M tokens\nm-1 | $123456789012 | $1,000,000 | $0.0000625 | $1.5 | $2 per 1K tokens")]
    # 123456789012 has twelve digits but is above the limit; 1,000,000 is the limit itself; $2 per 1K tokens is $2000 per 1M
    assert found == [Decimal(1_000_000), Decimal("0.0000625"), Decimal("1.5"), Decimal(2000)]


def test_the_work_on_one_page_is_bounded() -> None:
    text = "Per 1M tokens\nm-1 " + "$1 " * 1_700_000
    started = time.monotonic()
    found = pricefetch.find_prices(text)
    assert time.monotonic() - started < 3 and len(found) == pricefetch.MAX_PRICES
    table = parse_pricing({"models": {"m-1": {"input": 2, "output": 10}}})
    assert seconds(lambda: pricefetch.check_page(text, table, ["m-1"])) < 3
