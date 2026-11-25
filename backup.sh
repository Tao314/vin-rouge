#!/bin/bash
# vin-rouge v2 备份: 数据库 + 上传图片
set -e
cd "$(dirname "$0")"
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p backup
tar czf "backup/vin-rouge-$STAMP.tar.gz" vin_rouge.db uploads/ 2>/dev/null || tar czf "backup/vin-rouge-$STAMP.tar.gz" vin_rouge.db
echo "备份完成: backup/vin-rouge-$STAMP.tar.gz"
ls -lh backup/ | tail -5
