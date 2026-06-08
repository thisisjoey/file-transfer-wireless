#!/bin/zsh
# Usage: ./start.sh [directory] [port]
# Defaults: shares your home folder on port 8765

DIR="${1:-$HOME}"
PORT="${2:-8765}"

python3 "$(dirname "$0")/server.py" "$DIR" "$PORT"
