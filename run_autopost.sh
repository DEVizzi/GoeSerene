#!/bin/zsh
set -e

REPO_DIR="/Users/izazhaq/.gemini/jetski/scratch/GoeSerene"
cd "$REPO_DIR"

mkdir -p "$REPO_DIR/logs"
export PATH="$REPO_DIR/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

echo "============================================================" >> "$REPO_DIR/logs/autopost.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Starting GoeSerene Auto-Post..." >> "$REPO_DIR/logs/autopost.log"

"$REPO_DIR/.venv/bin/python" "$REPO_DIR/natGeo_bot.py" >> "$REPO_DIR/logs/autopost.log" 2>&1

# Clean up generated videos older than 2 days to save disk space
find "$REPO_DIR/videos" -name "*.mp4" -mtime +2 -delete 2>/dev/null || true

# Commit updated history.txt locally (and push if non-interactive credentials exist)
git add history.txt 2>/dev/null || true
git diff --cached --quiet || git commit -m "🤖 Update history after auto-post" >> "$REPO_DIR/logs/autopost.log" 2>&1 || true
GIT_TERMINAL_PROMPT=0 git push origin main >> "$REPO_DIR/logs/autopost.log" 2>&1 || true

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] Finished GoeSerene Auto-Post successfully." >> "$REPO_DIR/logs/autopost.log"
