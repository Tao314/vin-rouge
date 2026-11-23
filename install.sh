#!/usr/bin/env bash
# Install wine-system on Debian 13 (run as root). Idempotent.
# 仅供部署参考: 装 systemd 服务监听 127.0.0.1:9000, 前面放 Nginx/Caddy 反代 HTTPS。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

apt-get update -y
apt-get install -y python3 python3-venv python3-pip

if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

mkdir -p data/backups data/uploads

cat > /etc/systemd/system/wine-system.service <<EOF
[Unit]
Description=Wine Inventory MVP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn app:app --host 127.0.0.1 --port 9000
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable wine-system
systemctl restart wine-system
sleep 2
systemctl --no-pager status wine-system || true
echo
echo "Service listening on 127.0.0.1:9000. Put Nginx/Caddy in front for public HTTPS access."
echo "全新安装: 打开页面注册第一个账号即自动成为管理员; 老库升级/设了 WINE_ADMIN_PASSWORD 时管理员 allen314 初始密码见 $APP_DIR/data/INITIAL_PASSWORD.txt (登录后立即改密并删除该文件)。"
echo "Quick check: curl -s http://127.0.0.1:9000/api/me"
