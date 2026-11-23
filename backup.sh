#!/usr/bin/env bash
# 一键备份 SQLite(Online Backup 方式, WAL 下安全)。用法: ./backup.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${WINE_DATA_DIR:-$APP_DIR/data}"
mkdir -p "$DATA_DIR/backups"
OUT="$DATA_DIR/backups/wine-backup-$(date +%Y%m%d-%H%M%S).db"
python3 - "$DATA_DIR/wine.db" "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
print("backup ok:", sys.argv[2])
PY
# 只保留最近 10 份
ls -1t "$DATA_DIR"/backups/wine-backup-*.db 2>/dev/null | tail -n +11 | xargs -r rm -f
