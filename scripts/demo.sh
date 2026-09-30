#!/usr/bin/env bash
# Fixture demo: runs AgentFare on the synthetic sample logs in tests/fixtures and prints each command before its output.
#
#   bash scripts/demo.sh                   # live demo in a terminal
#   DEMO_PAUSE=0 bash scripts/demo.sh      # no pauses (this is what the tests use)
#   vhs demo.tape                          # recorded as a GIF, see docs/DEMO.md
#
# Nothing here reads your real agent logs or writes to ~/.agentfare: the data directory is a temporary one, "now" is
# frozen and the config is tests/fixtures/config.yaml, so the numbers are the same on every run and every machine.
#
# Settings (environment):
#   DEMO_PAUSE     seconds to wait after each step (default 2 in a terminal, 0 otherwise)
#   DEMO_CLEAR     1 clears the screen before each step (the VHS tape sets it), default 0
#   DEMO_TYPE      1 types each command out slowly, 0 prints it at once (default 1 in a terminal, 0 otherwise)
#   COLUMNS        output width (default: the terminal width, at most 100, and 80 when there is no terminal)
#   AGENTFARE_CMD  the command that starts AgentFare (default: "agentfare" when it is on PATH, else "$PYTHON -m agentfare")
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -t 1 ]; then
  interactive=1
else
  interactive=0
fi
pause="${DEMO_PAUSE:-$((interactive * 2))}"
clear_screen="${DEMO_CLEAR:-0}"
type_out="${DEMO_TYPE:-$interactive}"

if [ -z "${COLUMNS:-}" ]; then
  COLUMNS=80
  if [ "$interactive" = 1 ]; then
    cols="$(stty size 2>/dev/null </dev/tty | cut -d' ' -f2 || true)"
    [ -z "$cols" ] || COLUMNS="$cols"
  fi
  [ "$COLUMNS" -le 100 ] || COLUMNS=100
fi
export COLUMNS

AGENTFARE_HOME="$(mktemp -d)"
trap 'rm -rf "$AGENTFARE_HOME"' EXIT
export AGENTFARE_HOME
export AGENTFARE_NOW=2026-09-30T12:00:00Z
export AGENTFARE_CONFIG="$root/tests/fixtures/config.yaml"

# `agentfare` below is this function, so the command shown on screen is the command that runs.
agentfare() {
  if [ -n "${AGENTFARE_CMD:-}" ]; then
    $AGENTFARE_CMD "$@"
  elif type -P agentfare >/dev/null 2>&1; then  # -P: a program on PATH, not this function (`command -v` would find the function)
    command agentfare "$@"
  else
    "${PYTHON:-python3}" -m agentfare "$@"
  fi
}

if ! agentfare --version >/dev/null 2>&1; then
  echo "cannot start agentfare: run 'pip install -e .' from the repository root first" >&2
  exit 1
fi

# `agentfare run` counts the files of the current directory: a small one keeps the sample picks the same everywhere.
cd "$root/tests/fixtures"

show_typed() {
  local text="$1" i
  printf '$ '
  if [ "$type_out" = 1 ]; then
    for ((i = 0; i < ${#text}; i++)); do
      printf '%s' "${text:i:1}"
      sleep 0.03
    done
    sleep 0.4
    printf '\n'
  else
    printf '%s\n' "$text"
  fi
}

step() {
  if [ "$clear_screen" = 1 ]; then
    clear 2>/dev/null || printf '\033[2J\033[H'
  fi
  show_typed "$1"
  eval "$1"
  printf '\n'
  sleep "$pause"
}

step 'agentfare summary'
step 'agentfare sessions'
step 'agentfare advise'
step 'agentfare run --dry-run -- claude -p "fix the typo in the README"'
step 'agentfare run --dry-run -- claude -p "refactor the billing module and migrate it to the new API"'
step 'agentfare report --format md | sed -n 1,13p'

echo "That was the synthetic sample. Point AgentFare at your own logs with: agentfare summary"
