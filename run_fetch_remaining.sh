#!/usr/bin/env bash
# =============================================================================
# run_fetch_remaining.sh
# =============================================================================
# Manage the fetch_remaining.py background process.
#
# Usage:
#   ./run_fetch_remaining.sh start          # First run (full listing re-crawl)
#   ./run_fetch_remaining.sh resume         # Resume after stop/crash (skip crawl)
#   ./run_fetch_remaining.sh stop           # Gracefully stop the process
#   ./run_fetch_remaining.sh status         # Check if running + show tail of log
#   ./run_fetch_remaining.sh logs           # Live-stream the log
#   ./run_fetch_remaining.sh check          # Run compare_all.py coverage report
#
# Options you can override via environment variables before calling start/resume:
#   WORKERS=40 ./run_fetch_remaining.sh start
#   LISTING_WORKERS=30 ./run_fetch_remaining.sh start
#   CATEGORIES="Garten,Bad" ./run_fetch_remaining.sh start
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv/bin/activate"
PYTHON="$SCRIPT_DIR/venv/bin/python"
FETCHER="$SCRIPT_DIR/scratch/fetch_remaining.py"
STDOUT_LOG="$SCRIPT_DIR/fetch_remaining_stdout.log"
PID_FILE="$SCRIPT_DIR/.fetch_remaining.pid"

WORKERS="${WORKERS:-30}"
LISTING_WORKERS="${LISTING_WORKERS:-25}"
CATEGORIES="${CATEGORIES:-}"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

# ── Helpers ───────────────────────────────────────────────────────────────────
get_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        # Verify it's actually still running
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return
        fi
        rm -f "$PID_FILE"
    fi
    # Fallback: search by process name
    pgrep -f "fetch_remaining.py" 2>/dev/null | head -1 || true
}

is_running() {
    local pid
    pid=$(get_pid)
    [[ -n "$pid" ]]
}

build_args() {
    local args=("--workers" "$WORKERS" "--listing-workers" "$LISTING_WORKERS")
    if [[ -n "$CATEGORIES" ]]; then
        args+=("--categories" "$CATEGORIES")
    fi
    # Use ${@+"$@"} to avoid unbound variable if no arguments provided
    echo "${args[@]}" "${@+"$@"}"
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_start() {
    if is_running; then
        echo -e "${YELLOW}⚠  Already running (PID $(get_pid)). Use 'resume' or 'stop' first.${RESET}"
        exit 1
    fi

    echo -e "${CYAN}${BOLD}▶  Starting fetch_remaining.py (full listing re-crawl)…${RESET}"
    echo -e "   Workers        : ${WORKERS} product  /  ${LISTING_WORKERS} listing"
    [[ -n "$CATEGORIES" ]] && echo -e "   Categories     : ${CATEGORIES}" || echo -e "   Categories     : ALL"
    echo -e "   Log            : ${STDOUT_LOG}"
    echo ""

    # shellcheck disable=SC1090
    source "$VENV"

    # Build argument list
    local args_str
    args_str=$(build_args --refresh)
    read -ra ARGS <<< "$args_str"

    nohup "$PYTHON" "$FETCHER" "${ARGS[@]}" \
        > "$STDOUT_LOG" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo -e "${GREEN}✅ Started successfully (PID $pid)${RESET}"
        echo -e "   Monitor: ${CYAN}tail -f $STDOUT_LOG${RESET}"
        echo -e "   Stop:    ${CYAN}./run_fetch_remaining.sh stop${RESET}"
    else
        echo -e "${RED}❌ Process exited immediately — check log:${RESET}"
        tail -20 "$STDOUT_LOG"
        exit 1
    fi
}

cmd_resume() {
    if is_running; then
        echo -e "${YELLOW}⚠  Already running (PID $(get_pid)). Nothing to resume.${RESET}"
        exit 1
    fi

    echo -e "${CYAN}${BOLD}♻  Resuming fetch_remaining.py (skipping listing crawl, using cache)…${RESET}"
    echo -e "   Workers        : ${WORKERS} product  /  ${LISTING_WORKERS} listing"
    [[ -n "$CATEGORIES" ]] && echo -e "   Categories     : ${CATEGORIES}" || echo -e "   Categories     : ALL"
    echo -e "   Log            : ${STDOUT_LOG}"
    echo ""

    # shellcheck disable=SC1090
    source "$VENV"

    # No --refresh flag → uses .crawl_cache_full.json
    local args_str
    args_str=$(build_args)
    read -ra ARGS <<< "$args_str"

    nohup "$PYTHON" "$FETCHER" "${ARGS[@]}" \
        >> "$STDOUT_LOG" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        echo -e "${GREEN}✅ Resumed successfully (PID $pid)${RESET}"
        echo -e "   Monitor: ${CYAN}tail -f $STDOUT_LOG${RESET}"
    else
        echo -e "${RED}❌ Process exited immediately — check log:${RESET}"
        tail -20 "$STDOUT_LOG"
        exit 1
    fi
}

cmd_stop() {
    local pid
    pid=$(get_pid)

    if [[ -z "$pid" ]]; then
        echo -e "${YELLOW}⚠  No running process found.${RESET}"
        rm -f "$PID_FILE"
        exit 0
    fi

    echo -e "${CYAN}⏹  Stopping fetch_remaining.py (PID $pid)…${RESET}"
    kill "$pid" 2>/dev/null || true

    # Wait up to 10 seconds for graceful shutdown
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 10 ]]; do
        sleep 1
        (( waited++ ))
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo -e "${YELLOW}  Process not stopping — force killing…${RESET}"
        kill -9 "$pid" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"
    echo -e "${GREEN}✅ Stopped. Data flushed up to last checkpoint (every 150 rows).${RESET}"
    echo -e "   Resume anytime: ${CYAN}./run_fetch_remaining.sh resume${RESET}"
}

cmd_status() {
    local pid
    pid=$(get_pid)

    echo -e "${BOLD}═══════════════════════════════════════${RESET}"
    if [[ -n "$pid" ]]; then
        echo -e "${GREEN}● Running${RESET}  (PID $pid)"
        # Show CPU/memory usage
        ps -o pid,pcpu,pmem,etime,command -p "$pid" 2>/dev/null | tail -1 || true
    else
        echo -e "${RED}● Not running${RESET}"
    fi
    echo -e "${BOLD}═══════════════════════════════════════${RESET}"
    echo ""
    echo -e "${CYAN}Last 20 lines of log:${RESET}"
    if [[ -f "$STDOUT_LOG" ]]; then
        tail -20 "$STDOUT_LOG"
    else
        echo "  (log file not found)"
    fi
}

cmd_logs() {
    if [[ ! -f "$STDOUT_LOG" ]]; then
        echo -e "${RED}❌ Log file not found: $STDOUT_LOG${RESET}"
        exit 1
    fi
    echo -e "${CYAN}Streaming log (Ctrl+C to stop — process keeps running):${RESET}"
    echo ""
    tail -f "$STDOUT_LOG"
}

cmd_check() {
    echo -e "${CYAN}${BOLD}📊 Running coverage check (compare_all.py)…${RESET}"
    echo ""
    # shellcheck disable=SC1090
    source "$VENV"
    "$PYTHON" "$SCRIPT_DIR/scratch/compare_all.py"
}

cmd_help() {
    echo -e "${BOLD}Usage:${RESET}  ./run_fetch_remaining.sh <command> [env overrides]"
    echo ""
    echo -e "${BOLD}Commands:${RESET}"
    echo -e "  ${GREEN}start${RESET}    Full listing re-crawl + fetch all missing products"
    echo -e "  ${GREEN}resume${RESET}   Resume from cache after a stop or crash"
    echo -e "  ${GREEN}stop${RESET}     Gracefully stop the background process"
    echo -e "  ${GREEN}status${RESET}   Show running status + last 20 log lines"
    echo -e "  ${GREEN}logs${RESET}     Live-stream the log (Ctrl+C to exit)"
    echo -e "  ${GREEN}check${RESET}    Run compare_all.py to show coverage report"
    echo ""
    echo -e "${BOLD}Environment overrides:${RESET}"
    echo -e "  WORKERS=40           Number of product-fetch workers  (default: 30)"
    echo -e "  LISTING_WORKERS=30   Number of listing-crawl workers  (default: 25)"
    echo -e "  CATEGORIES=\"Garten,Bad\"  Only process specific categories"
    echo ""
    echo -e "${BOLD}Examples:${RESET}"
    echo -e "  ./run_fetch_remaining.sh start"
    echo -e "  WORKERS=40 ./run_fetch_remaining.sh start"
    echo -e "  CATEGORIES=\"Garten,Innendeko\" ./run_fetch_remaining.sh start"
    echo -e "  ./run_fetch_remaining.sh resume"
    echo -e "  ./run_fetch_remaining.sh stop"
    echo -e "  ./run_fetch_remaining.sh logs"
    echo -e "  ./run_fetch_remaining.sh check"
}

# ── Entry point ───────────────────────────────────────────────────────────────
case "${1:-help}" in
    start)   cmd_start  ;;
    resume)  cmd_resume ;;
    stop)    cmd_stop   ;;
    status)  cmd_status ;;
    logs)    cmd_logs   ;;
    check)   cmd_check  ;;
    *)       cmd_help   ;;
esac
