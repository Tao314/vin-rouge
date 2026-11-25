#!/bin/bash
# vin-rouge v2 部署到 /opt/vin-rouge (systemd 服务 vin-rouge)
# 用法: 在服务器上 git pull 后执行 ./install.sh
set -e
# root 下不用 sudo，非 root 才用 sudo
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
cd "$(dirname "$0")"
$SUDO mkdir -p /opt/vin-rouge
if [ "$(pwd)" != "/opt/vin-rouge" ]; then $SUDO cp -r app.py static requirements.txt install.sh backup.sh /opt/vin-rouge/; fi
cd /opt/vin-rouge
python3 -m venv .venv 2>/dev/null || true
./.venv/bin/pip install -q -r requirements.txt
$SUDO tee /etc/systemd/system/vin-rouge.service > /dev/null <<'EOF'
[Unit]
Description=vin-rouge v2 酒类分销
After=network.target

[Service]
WorkingDirectory=/opt/vin-rouge
ExecStart=/opt/vin-rouge/.venv/bin/uvicorn app:app --host 127.0.0.1 --port ${PORT}
Restart=always
Environment=PORT=9000
Environment=BARCODE_API_URL=
Environment=BARCODE_API_KEY=

[Install]
WantedBy=multi-user.target
EOF
$SUDO systemctl daemon-reload
$SUDO systemctl enable vin-rouge
$SUDO systemctl restart vin-rouge
echo "部署完成, 服务状态:"
$SUDO systemctl status vin-rouge --no-pager | head -10
