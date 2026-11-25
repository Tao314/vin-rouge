#!/bin/bash
# vin-rouge v2 部署到 /opt/vin-rouge (systemd 服务 vin-rouge)
# 用法: 在服务器上 git pull 后执行 ./install.sh
set -e
cd "$(dirname "$0")"
sudo mkdir -p /opt/vin-rouge
sudo cp -r app.py static requirements.txt install.sh backup.sh /opt/vin-rouge/
cd /opt/vin-rouge
python3 -m venv .venv 2>/dev/null || true
./.venv/bin/pip install -q -r requirements.txt
sudo tee /etc/systemd/system/vin-rouge.service > /dev/null <<'EOF'
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
sudo systemctl daemon-reload
sudo systemctl enable vin-rouge
sudo systemctl restart vin-rouge
echo "部署完成, 服务状态:"
sudo systemctl status vin-rouge --no-pager | head -10
