#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_local_scraper.sh – Hornbach Local Mirror Scraper Runner
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║    🧬  Hornbach Local Mirror Specification Scraper       ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Virtualenv ───────────────────────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/venv"
if [ -d "$VENV_DIR" ]; then
    echo -e "${GREEN}✓ Activating virtual environment…${NC}"
    source "$VENV_DIR/bin/activate"
else
    echo -e "${YELLOW}⚠ No venv found at $VENV_DIR. Trying system python3.${NC}"
fi

# ── Run ──────────────────────────────────────────────────────────────────────
echo -e "${CYAN}▶ Starting local extraction from mirror…${NC}"
echo ""

python3 local_spec_scraper.py

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}${BOLD}✅ Local extraction finished successfully!${NC}"
    echo -e "${CYAN}📁 Check output in: ${BOLD}output_local/specs/${NC}"
else
    echo -e "${YELLOW}⚠ Local extraction failed with code $EXIT_CODE${NC}"
fi

echo ""
