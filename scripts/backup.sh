#!/bin/sh
# vin-rouge 数据库在线备份.
# 必须用 sqlite3 .backup 在线备份正在使用的数据库 (兼顾 WAL, 不能直接 cp
# 数据库文件, 否则可能拷到一半写入的库). 备份后自动验证, 验证通过后才
# 清理旧备份. 保留策略: 每天 7 份 + 每周一份留 4 周.
# 环境变量: VIN_ROUGE_DB (默认脚本所在目录上一级的 vin_rouge.db),
# VIN_ROUGE_BACKUP_DIR (默认脚本所在目录上一级的 backups/).
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DB="${VIN_ROUGE_DB:-$SCRIPT_DIR/../vin_rouge.db}"
BACKUP_ROOT="${VIN_ROUGE_BACKUP_DIR:-$SCRIPT_DIR/../backups}"
DAILY_DIR="$BACKUP_ROOT/daily"
WEEKLY_DIR="$BACKUP_ROOT/weekly"

command -v sqlite3 >/dev/null 2>&1 || { echo "备份失败: 找不到 sqlite3, 请先安装 (apt-get install -y sqlite3)"; exit 1; }
[ -f "$DB" ] || { echo "备份失败: 数据库不存在: $DB"; exit 1; }
mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

TS=$(date +%Y%m%d-%H%M%S)
NAME="vin_rouge-$TS.db"
DEST="$DAILY_DIR/$NAME"

# 1. 在线备份 (sqlite3 .backup, 正在跑的库也能安全备份)
sqlite3 "$DB" ".backup '$DEST'"

# 2. 自动验证: 文件存在、大小非 0、完整性检查通过、核心表存在
[ -s "$DEST" ] || { echo "备份失败: 备份文件为空: $DEST"; rm -f "$DEST"; exit 1; }
CHK=$(sqlite3 "$DEST" "PRAGMA integrity_check;")
[ "$CHK" = "ok" ] || { echo "备份失败: 完整性检查不通过: $CHK"; rm -f "$DEST"; exit 1; }
for t in users wines warehouses stock inbound_orders outbound_orders transfers payments return_orders; do
    n=$(sqlite3 "$DEST" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$t';")
    [ "$n" = "1" ] || { echo "备份失败: 核心表缺失: $t"; rm -f "$DEST"; exit 1; }
done

# 3. 每周一份 (周日) 另外留一份, 保留 4 周
if [ "$(date +%u)" = "7" ]; then
    cp "$DEST" "$WEEKLY_DIR/$NAME"
fi

# 4. 验证通过后才清理旧备份: 每天的留 7 天, 每周的留 4 周 (28 天)
find "$DAILY_DIR" -name 'vin_rouge-*.db' -type f -mtime +7 -delete
find "$WEEKLY_DIR" -name 'vin_rouge-*.db' -type f -mtime +28 -delete

SIZE=$(wc -c < "$DEST")
echo "备份完成: $DEST (${SIZE} 字节, 完整性检查 ok)"
