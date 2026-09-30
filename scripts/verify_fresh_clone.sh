#!/usr/bin/env bash
# Prove that a fresh clone of AgentFare works, on every Python interpreter that is available.
#
#   scripts/verify_fresh_clone.sh [--worktree] [REPO_PATH_OR_URL]
#
# What it does (nothing touches your working tree, ~/.agentfare or the real agent logs):
#   1. git clone the repository (default: the repository this script lives in; a path or a URL may be given) into a
#      temporary directory. Only COMMITTED files are in a clone: commit first to test new files.
#      With --worktree it copies the working tree of a local directory instead (no git is run; uncommitted files are
#      included; .git, .venv, caches and build output are left out). That is a rehearsal for before the commit: the
#      proof of a fresh clone is the run without the flag.
#   2. For each interpreter among python3.11 python3.12 python3.13 python3.14 python3 (missing ones are skipped and said
#      so; duplicates and versions below 3.11 are skipped too): a fresh virtualenv, `pip install -e '.[dev]'`,
#      `agentfare --help` and `agentfare --version`, the fixture demo (`summary` and `advise` on tests/fixtures with a
#      throwaway AGENTFARE_HOME and a frozen AGENTFARE_NOW) whose numbers must equal the ones in
#      tests/fixtures/EXPECTED.md (parsed from that file, not repeated here), and `pytest -q`.
#   3. A PASS/FAIL table, cleanup of every temporary directory, exit status 1 if anything failed.
#
# Environment (all optional):
#   VERIFY_PYTHONS   interpreters to try, instead of "python3.11 python3.12 python3.13 python3.14 python3"
#   VERIFY_PIP_ARGS  extra arguments for `pip install` (e.g. "--no-index --find-links /path/to/wheels" to work offline)
#   VERIFY_TMPDIR    where the temporary directory goes (default: /tmp, else $TMPDIR; keep the path short: a checkout under a
#                    long path breaks one unrelated test, see the comment at WORK below)
#   KEEP_WORKDIR=1   keep the temporary directory (its path is printed) for debugging
#
# `pip install` needs a package index (PyPI by default): it is the only network use, and AgentFare itself uses none.

set -euo pipefail

MODE=clone
ARG_SRC=""
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --worktree) MODE=worktree ;;
    -*) echo "verify_fresh_clone.sh: unknown option $arg (see --help)" >&2; exit 2 ;;
    *)
      if [[ -n "$ARG_SRC" ]]; then echo "verify_fresh_clone.sh: only one repository may be given (see --help)" >&2; exit 2; fi
      ARG_SRC="$arg"
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${ARG_SRC:-$(cd "$SCRIPT_DIR/.." && pwd)}"
if [[ -d "$SRC" ]]; then
  SRC="$(cd "$SRC" && pwd)"  # a local path: make it absolute, git clone runs elsewhere
fi
if [[ "$MODE" == worktree && ! -d "$SRC" ]]; then
  echo "verify_fresh_clone.sh: --worktree needs a local directory, not '$SRC'" >&2
  exit 2
fi

FROZEN_NOW="2026-09-30T12:00:00Z"
COMMANDS="ingest config summary sessions models advise run report prices"
CANDIDATES="${VERIFY_PYTHONS:-python3.11 python3.12 python3.13 python3.14 python3}"

# The work directory is short on purpose (VERIFY_TMPDIR, else /tmp, else $TMPDIR): `agentfare prices` prints the absolute
# path of its price table, and tests/test_prices_cmd.py::test_output_is_plain_text_that_fits_80_columns fails when that
# line passes 80 columns, i.e. for a checkout whose path is longer than about 40 characters (macOS's /var/folders/...,
# a deep home directory). That is a flaw of that test or of that output, not of this script: a short path keeps it from
# hiding the other results.
if [[ -n "${VERIFY_TMPDIR:-}" ]]; then BASE_TMP="$VERIFY_TMPDIR"; elif [[ -d /tmp && -w /tmp ]]; then BASE_TMP=/tmp; else BASE_TMP="${TMPDIR:-.}"; fi
WORK="$(mktemp -d "${BASE_TMP%/}/afc.XXXXXX")"
cleanup() {
  if [[ "${KEEP_WORKDIR:-0}" == "1" ]]; then
    echo "kept: $WORK"
  else
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# ---- results table ---------------------------------------------------------------------------
ROW_STATUS=(); ROW_WHO=(); ROW_STEP=(); ROW_NOTE=()
ROWS=0
FAILS=0

record() {  # record STATUS WHO STEP NOTE
  ROW_STATUS[ROWS]="$1"; ROW_WHO[ROWS]="$2"; ROW_STEP[ROWS]="$3"; ROW_NOTE[ROWS]="${4:-}"
  ROWS=$((ROWS + 1))
  if [[ "$1" == "FAIL" ]]; then FAILS=$((FAILS + 1)); fi
  printf '  %-4s  %s: %s%s\n' "$1" "$2" "$3" "${4:+ ($4)}"
}

show_log() {  # show_log FILE [LINES]: the tail of a log, without pytest's progress lines (only dots and a percentage)
  if [[ -s "$1" ]]; then
    echo "  ---- last lines of $(basename "$1") ----"
    { grep -v -E '^[.FEsxX]+( +\[ *[0-9]+%\])?$' "$1" || true; } | tail -n "${2:-25}" | sed 's/^/  | /'
    echo "  ----"
  fi
}

# ---- expected numbers, parsed from the clone's EXPECTED.md ------------------------------------------
expected() {  # expected KEY  ->  the value of "KEY = value" in the machine-readable block
  awk -v key="$1" 'index($0, key " = ") == 1 { print substr($0, length(key) + 4); exit }' "$CLONE/tests/fixtures/EXPECTED.md"
}

json_get() {  # json_get PYTHON EXPRESSION  (JSON on stdin; EXPRESSION uses the name d)
  "$1" -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1]))' "$2"
}

# ---- 1. clone (or, with --worktree, a copy of the working tree) --------------------------------------
echo "== repository: $SRC${MODE:+ (mode: $MODE)}"
echo "== work dir:   $WORK"
CLONE="$WORK/repo"
count_files() { find "$CLONE" -path "$CLONE/.git" -prune -o -type f -print | wc -l | tr -d ' '; }
if [[ "$MODE" == worktree ]]; then
  # Top-level entries one by one, so a big .venv is never copied; nested caches and build output are pruned after.
  mkdir -p "$CLONE"
  for entry in "$SRC"/* "$SRC"/.[!.]*; do
    [[ -e "$entry" || -L "$entry" ]] || continue
    case "$(basename "$entry")" in
      .git|.venv|venv|build|dist|.agentfare|.pytest_cache|.mypy_cache|.ruff_cache|htmlcov|.coverage|.DS_Store|*.egg-info|*.db) continue ;;
    esac
    cp -R "$entry" "$CLONE/"
  done
  find "$CLONE" \( -name __pycache__ -o -name '*.egg-info' -o -name .pytest_cache \) -prune -exec rm -rf {} + 2>/dev/null || true
  find "$CLONE" -name '*.pyc' -delete 2>/dev/null || true
  record PASS copy "working tree" "uncommitted files included, $(count_files) files (a rehearsal: the real proof is a clone of a commit)"
else
  if ! GIT_TERMINAL_PROMPT=0 git clone --quiet --no-hardlinks "$SRC" "$CLONE" >"$WORK/clone.log" 2>&1; then
    record FAIL clone "git clone $SRC" "see below"
    show_log "$WORK/clone.log"
    echo
    echo "RESULT: FAIL (nothing to test without a clone)"
    exit 1
  fi
  record PASS clone "git clone" "committed files only: $(count_files) files"
fi

EXPECTED_MD="$CLONE/tests/fixtures/EXPECTED.md"
EXP_TOTAL_UCENTS=""; EXP_TOTAL_USD=""; EXP_SAVED_UCENTS=""; EXP_SAVED_USD=""; EXP_SWITCH_LINE=""
if [[ -f "$EXPECTED_MD" ]]; then
  EXP_TOTAL_UCENTS="$(expected summary.total_ucents)"
  EXP_TOTAL_USD="$(expected summary.total_usd)"
  EXP_SAVED_UCENTS="$(expected advise.saved_ucents)"
  EXP_SAVED_USD="$(expected advise.saved_usd)"
  EXP_SWITCH_LINE="$(expected advise.switch_line)"
fi
if [[ -z "$EXP_TOTAL_UCENTS" || -z "$EXP_SAVED_UCENTS" || -z "$EXP_SWITCH_LINE" ]]; then
  record FAIL expectations "tests/fixtures/EXPECTED.md" "missing from the $MODE, or it has no machine-readable block (is it committed?)"
  HAVE_EXPECTED=0
else
  record PASS expectations "tests/fixtures/EXPECTED.md" "total $EXP_TOTAL_USD ($EXP_TOTAL_UCENTS ucents), saved $EXP_SAVED_USD"
  HAVE_EXPECTED=1
fi
PROJECT_VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$CLONE/pyproject.toml" | head -n 1)"

# ---- 2. one interpreter -------------------------------------------------------------------------
verify() {  # verify NAME PATH TAG
  local name="$1" path="$2" tag="$3" venv vpy who out missing=""
  who="$name"
  venv="$WORK/venv-$tag"

  if ! "$path" -m venv "$venv" >"$WORK/venv-$tag.log" 2>&1; then
    record FAIL "$who" "create venv" "python -m venv failed"
    show_log "$WORK/venv-$tag.log"
    return
  fi
  vpy="$venv/bin/python"
  record PASS "$who" "create venv" "$("$vpy" -c 'import sys; print(sys.version.split()[0])')"

  # pip install -e '.[dev]' from the clone
  # shellcheck disable=SC2086  # VERIFY_PIP_ARGS is meant to split into words
  if ! (cd "$CLONE" && "$vpy" -m pip install --quiet --disable-pip-version-check ${VERIFY_PIP_ARGS:-} -e '.[dev]') >"$WORK/pip-$tag.log" 2>&1; then
    record FAIL "$who" "pip install -e '.[dev]'" "see below"
    show_log "$WORK/pip-$tag.log"
    return
  fi
  record PASS "$who" "pip install -e '.[dev]'"

  # agentfare --help lists every command
  if out="$("$venv/bin/agentfare" --help 2>&1)" && grep -q 'Usage' <<<"$out" \
     && missing="$(for c in $COMMANDS; do grep -q "$c" <<<"$out" || printf '%s ' "$c"; done)" && [[ -z "$missing" ]]; then
    record PASS "$who" "agentfare --help" "lists the 9 commands"
  else
    record FAIL "$who" "agentfare --help" "exit status or output wrong${missing:+; missing: $missing}"
    printf '%s\n' "${out:-}" | head -n 15 | sed 's/^/  | /'
  fi

  # agentfare --version equals the version in pyproject.toml
  if out="$("$venv/bin/agentfare" --version 2>&1)" && [[ "$out" == "agentfare $PROJECT_VERSION" ]]; then
    record PASS "$who" "agentfare --version" "$out"
  else
    record FAIL "$who" "agentfare --version" "printed '$(printf '%s' "${out:-nothing}" | tail -n 1 | cut -c1-80)', pyproject.toml says '$PROJECT_VERSION'"
    printf '%s\n' "${out:-}" | head -n 15 | sed 's/^/  | /'
  fi

  # the fixture demo: same numbers as EXPECTED.md, on a throwaway data dir and a frozen clock
  local home="$WORK/home-$tag" userhome="$WORK/userhome-$tag"
  mkdir -p "$home" "$userhome"
  demo() {  # demo ARGS...  (run in the clone, isolated from the real machine)
    (cd "$CLONE" && HOME="$userhome" AGENTFARE_HOME="$home" AGENTFARE_NOW="$FROZEN_NOW" COLUMNS=100 PYTHONUTF8=1 \
      "$venv/bin/agentfare" --config tests/fixtures/config.yaml "$@")
  }
  if [[ "$HAVE_EXPECTED" == 1 ]]; then
    local got_ucents got_text
    if got_ucents="$(demo summary --json 2>"$WORK/demo-$tag.err" | json_get "$vpy" 'd["total"]["ucents"]')" \
       && got_text="$(demo summary 2>>"$WORK/demo-$tag.err")" \
       && [[ "$got_ucents" == "$EXP_TOTAL_UCENTS" ]] && grep -qF "total spend $EXP_TOTAL_USD" <<<"$got_text"; then
      record PASS "$who" "demo: summary total" "$EXP_TOTAL_USD = $got_ucents ucents, as in EXPECTED.md"
    else
      record FAIL "$who" "demo: summary total" "got '${got_ucents:-nothing}' ucents, EXPECTED.md says $EXP_TOTAL_UCENTS ($EXP_TOTAL_USD)"
      show_log "$WORK/demo-$tag.err" 10
    fi
    local saved line advise_text
    if saved="$(demo advise --json 2>"$WORK/demo-$tag.err" | json_get "$vpy" 'd["total"]["saved"]["ucents"]')" \
       && line="$(demo advise --json 2>>"$WORK/demo-$tag.err" | json_get "$vpy" 'd["sessions"][0]["line"]')" \
       && advise_text="$(demo advise 2>>"$WORK/demo-$tag.err")" \
       && [[ "$saved" == "$EXP_SAVED_UCENTS" && "$line" == "$EXP_SWITCH_LINE" ]] && grep -qF "$EXP_SAVED_USD" <<<"$advise_text"; then
      record PASS "$who" "demo: advise savings" "$EXP_SAVED_USD saved, as in EXPECTED.md"
    else
      record FAIL "$who" "demo: advise savings" "got '${saved:-nothing}' ucents saved and '${line:-nothing}'; EXPECTED.md says $EXP_SAVED_UCENTS and '$EXP_SWITCH_LINE'"
      show_log "$WORK/demo-$tag.err" 10
    fi
  else
    record FAIL "$who" "demo: summary and advise" "no expected numbers to compare with"
  fi

  # the whole test suite, from the clone. TMPDIR points inside the work directory, so pytest's own temporary
  # directories (pytest-of-<user>/...) are removed with everything else instead of piling up in /tmp.
  local tests_log="$WORK/pytest-$tag.log" pytmp="$WORK/pytmp-$tag"
  mkdir -p "$pytmp"
  # `-o addopts=` because pyproject.toml already passes -q: a second -q would hide the "N passed" summary line
  if (cd "$CLONE" && TMPDIR="$pytmp" "$vpy" -m pytest -q -o addopts= -p no:cacheprovider) >"$tests_log" 2>&1; then
    record PASS "$who" "pytest -q" "$(tail -n 1 "$tests_log")"
  else
    record FAIL "$who" "pytest -q" "$(tail -n 1 "$tests_log")"
    show_log "$tests_log" 40
  fi
}

# ---- 3. every interpreter that exists -------------------------------------------------------------
echo "== interpreters: $CANDIDATES"
SEEN=""
RAN=0
for name in $CANDIDATES; do
  if ! path="$(command -v "$name" 2>/dev/null)"; then
    echo "  skip  $name: not found"
    continue
  fi
  if ! version="$("$path" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"; then
    echo "  skip  $name: $path does not run"
    continue
  fi
  if ! "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "  skip  $name: Python $version is older than 3.11 (pyproject.toml: requires-python >= 3.11)"
    continue
  fi
  real="$("$path" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
  case "$SEEN" in
    *"|$real|"*) echo "  skip  $name: same interpreter as one already tested ($real)"; continue ;;
  esac
  SEEN="$SEEN|$real|"
  RAN=$((RAN + 1))
  echo "  run   $name -> $path (Python $version)"
  verify "$name (Python $version)" "$path" "$RAN-$version" || true
done
if [[ "$RAN" == 0 ]]; then
  record FAIL interpreters "none of: $CANDIDATES" "no Python 3.11 or newer found"
fi

# ---- 4. the table ---------------------------------------------------------------------------------
echo
echo "== RESULTS ($RAN interpreter(s))"
printf '%-6s %-28s %-26s %s\n' STATUS WHO STEP NOTE
i=0
while [[ "$i" -lt "$ROWS" ]]; do
  printf '%-6s %-28s %-26s %s\n' "${ROW_STATUS[$i]}" "${ROW_WHO[$i]}" "${ROW_STEP[$i]}" "${ROW_NOTE[$i]}"
  i=$((i + 1))
done
echo
if [[ "$FAILS" -gt 0 ]]; then
  echo "RESULT: FAIL ($FAILS of $ROWS checks failed)"
  exit 1
fi
echo "RESULT: PASS (all $ROWS checks on $RAN interpreter(s))"
