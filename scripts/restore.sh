#!/bin/sh
# vin-rouge 备份恢复演练: 把指定备份文件恢复到临时目录并做完整性检查,
# 不覆盖正在跑的数据库.
# 用法: restore.sh <备份文件> [恢复目录, 默认新建临时目录]
# 输出每张表的行数, 可与原库行数逐项对照.
set -eu
SRC="${1:-}"
[ -n "$SRC" ] || { echo "用法: $0 <备份文件> [恢复目录]"; exit 2; }
[ -f "$SRC" ] || { echo "备份文件不存在: $SRC"; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "恢复失败: 找不到 sqlite3"; exit 1; }

DEST_DIR="${2:-$(mktemp -d /tmp/vin-rouge-restore-XXXXXX)}"
mkdir -p "$DEST_DIR"
DEST="$DEST_DIR/vin_rouge.db"

# 从备份文件在线恢复出一份副本 (不碰正在跑的数据库)
sqlite3 "$SRC" ".backup '$DEST'"

[ -s "$DEST" ] || { echo "恢复失败: 恢复出的文件为空"; exit 1; }
CHK=$(sqlite3 "$DEST" "PRAGMA integrity_check;")
[ "$CHK" = "ok" ] || { echo "恢复失败: 完整性检查不通过: $CHK"; exit 1; }

# 核心表必须存在, 其余表有则计数
for t in users wines warehouses suppliers customers stock inbound_orders inbound_items outbound_orders outbound_items transfers order_confirms payments return_orders return_items audit_logs; do
    n=$(sqlite3 "$DEST" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$t';")
    if [ "$n" = "1" ]; then
        echo "$t $(sqlite3 "$DEST" "SELECT COUNT(*) FROM $t;")"
    else
        echo "$t MISSING"
    fi
done
for t in users wines warehouses stock inbound_orders outbound_orders; do
    n=$(sqlite3 "$DEST" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$t';")
    [ "$n" = "1" ] || { echo "恢复失败: 核心表缺失: $t"; exit 1; }
done
echo "恢复完成 (未覆盖正在跑的数据库): $DEST"
