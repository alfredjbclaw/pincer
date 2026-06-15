#!/usr/bin/env bash
# pincer keeper — meta-runner for the Mission → Goal → Control pipeline.
#
# Usage:
#   keeper audit  <owner/repo>     # Mission tier only
#   keeper triage <owner/repo>     # Goal tier only
#   keeper run    <owner/repo>     # Full continuous loop
#   keeper run    --all            # Every [[repos]] entry
#   keeper status                  # Current state + recent log entries
#   keeper stop                    # Stop continuous run cleanly
#
# Environment:
#   PINCER_CONFIG    — path to pincer.toml (default: ~/.openclaw/pincer.toml)
#   PINCER_HOME      — state root (default: ~/.openclaw/pincer)
set -euo pipefail

PINCER_CONFIG="${PINCER_CONFIG:-$HOME/.openclaw/pincer.toml}"
PINCER_HOME="${PINCER_HOME:-$HOME/.openclaw/pincer}"
PINCER_REPO_ROOT="${PINCER_REPO_ROOT:-$HOME/Projects/pincer}"
LOG_FILE="$PINCER_HOME/log.md"
STATE_FILE="$PINCER_HOME/state.json"

mkdir -p "$PINCER_HOME/plans"

usage() {
  cat <<'USAGE'
pincer keeper — meta-runner for the Mission → Goal → Control pipeline.

Usage:
  keeper audit  <owner/repo>     Mission tier only
  keeper triage <owner/repo>     Goal tier only
  keeper run    <owner/repo>     Full continuous loop (5m cadence)
  keeper run    --all            Every [[repos]] entry
  keeper status                  Current state + recent log entries
  keeper stop                    Stop continuous run cleanly

Environment:
  PINCER_CONFIG    path to pincer.toml (default: ~/.openclaw/pincer.toml)
  PINCER_HOME      state root          (default: ~/.openclaw/pincer)
USAGE
}

die() {
  echo "keeper: $*" >&2
  exit 1
}

require_config() {
  [ -f "$PINCER_CONFIG" ] || die "config not found: $PINCER_CONFIG (copy from $PINCER_REPO_ROOT/config.toml.example)"
}

require_crabbox() {
  command -v crabbox >/dev/null 2>&1 \
    || die "crabbox not installed. Run: brew install openclaw/tap/crabbox"
  if ! crabbox doctor >/dev/null 2>&1; then
    echo "warning: 'crabbox doctor' reported issues. Run it manually for details." >&2
  fi
}

require_runtime() {
  command -v codex >/dev/null 2>&1 \
    || command -v acpx >/dev/null 2>&1 \
    || die "no worker runtime found. Need either 'codex' or 'acpx' (Claude Code) on PATH."
}

log_event() {
  local tier="$1"; shift
  local msg="$*"
  printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tier" "$msg" >> "$LOG_FILE"
}

cmd_audit() {
  local repo="${1:-}"
  [ -n "$repo" ] || die "usage: keeper audit <owner/repo>"
  require_config
  log_event mission "audit start $repo"
  echo "Running audit-and-plan on $repo (Mission tier, Opus)..."
  echo "  → reads code, CI, deps, changelog"
  echo "  → writes ~/.openclaw/pincer/plans/${repo//\//-}-$(date -u +%Y-%m-%d).toml"
  echo
  echo "(Invoke the audit-and-plan skill from the OpenClaw runtime to actually run it."
  echo " keeper is a scaffolding wrapper; the skill itself is the work.)"
  log_event mission "audit handoff $repo"
}

cmd_triage() {
  local repo="${1:-}"
  [ -n "$repo" ] || die "usage: keeper triage <owner/repo>"
  require_config
  log_event goal "triage start $repo"
  echo "Running triage on $repo (Goal tier, Sonnet)..."
  local latest_plan
  latest_plan=$(ls -t "$PINCER_HOME/plans/${repo//\//-}-"*.toml 2>/dev/null | head -1 || true)
  if [ -z "$latest_plan" ]; then
    echo "  warning: no plan found for $repo. Run 'keeper audit $repo' first." >&2
  else
    echo "  → plan: $latest_plan"
  fi
  log_event goal "triage handoff $repo plan=${latest_plan:-none}"
}

cmd_run() {
  local arg="${1:-}"
  [ -n "$arg" ] || die "usage: keeper run <owner/repo> | --all"
  require_config
  require_crabbox
  require_runtime
  if [ "$arg" = "--all" ]; then
    echo "Starting orchestrator for every [[repos]] entry in $PINCER_CONFIG..."
    log_event control "run --all start"
  else
    echo "Starting orchestrator for $arg (Control tier, Sonnet, 5m cadence)..."
    log_event control "run start $arg"
  fi
  echo
  echo "  taskName: pincer-${arg//[\/\.]/-}"
  echo "  state:    $STATE_FILE"
  echo "  log:      $LOG_FILE"
  echo
  echo "(Spawn the orchestrator skill via sessions_spawn from the OpenClaw runtime."
  echo " keeper is the operator surface; the actual control loop is the skill.)"
}

cmd_status() {
  require_config
  echo "pincer keeper — state @ $(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo
  echo "Config:  $PINCER_CONFIG"
  echo "State:   $STATE_FILE"
  echo "Log:     $LOG_FILE"
  echo
  if [ -f "$LOG_FILE" ]; then
    echo "Recent log entries:"
    tail -n 10 "$LOG_FILE" | sed 's/^/  /'
  else
    echo "(log is empty — no runs yet)"
  fi
  echo
  if [ -f "$STATE_FILE" ]; then
    echo "State snapshot:"
    head -c 2000 "$STATE_FILE" | sed 's/^/  /'
    echo
  fi
}

cmd_stop() {
  log_event control "stop requested"
  echo "Sending shutdown signal to orchestrator session..."
  echo "(Use sessions_send or session_yield in the OpenClaw runtime to actually stop the session.)"
}

case "${1:-}" in
  audit)   shift; cmd_audit  "$@" ;;
  triage)  shift; cmd_triage "$@" ;;
  run)     shift; cmd_run    "$@" ;;
  status)  shift; cmd_status "$@" ;;
  stop)    shift; cmd_stop   "$@" ;;
  -h|--help|help|"")
    usage
    exit 0
    ;;
  *)
    die "unknown command: $1"
    ;;
esac
