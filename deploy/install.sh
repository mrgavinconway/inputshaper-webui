#!/bin/bash
set -euo pipefail
# Run as root inside a Debian 13 unprivileged LXC.
test "$(id -u)" = 0
APP_SOURCE=$(cd "$(dirname "$0")/.." && pwd)
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv git sudo curl
id inputshaper >/dev/null 2>&1 || useradd --system --home /var/lib/inputshaper --shell /usr/sbin/nologin inputshaper
install -d -m 755 /opt/inputshaper/releases /var/lib/inputshaper-update /usr/local/lib/inputshaper
install -d -o inputshaper -g inputshaper -m 750 /var/lib/inputshaper
install -m 755 "$APP_SOURCE/deploy/update.py" /usr/local/lib/inputshaper/update.py
if [ ! -f /etc/inputshaper.env ]; then
  umask 077
  python3 - <<'PY' > /etc/inputshaper.env
import secrets
print('INPUTSHAPER_PASSWORD='+secrets.token_urlsafe(18))
print('INPUTSHAPER_DATA=/var/lib/inputshaper')
print('MPLCONFIGDIR=/var/lib/inputshaper/matplotlib')
PY
fi
cat > /etc/systemd/system/inputshaper.service <<'EOF'
[Unit]
Description=Input Shaper Lab
After=network-online.target
Wants=network-online.target
[Service]
User=inputshaper
Group=inputshaper
WorkingDirectory=/opt/inputshaper/current
EnvironmentFile=/etc/inputshaper.env
ExecStart=/opt/inputshaper/current/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/var/lib/inputshaper
UMask=0027
[Install]
WantedBy=multi-user.target
EOF
for action in check update; do
  mode=check
  [ "$action" = update ] && mode=install
  cat > "/etc/systemd/system/inputshaper-$action.service" <<EOF
[Unit]
Description=Input Shaper Lab repository $action
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/lib/inputshaper/update.py $mode
TimeoutStartSec=1800
EOF
done
cat > /etc/systemd/system/inputshaper-check.timer <<'EOF'
[Unit]
Description=Check Input Shaper Lab updates every 15 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
RandomizedDelaySec=30
Persistent=true
[Install]
WantedBy=timers.target
EOF
cat > /etc/sudoers.d/inputshaper <<'EOF'
inputshaper ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block inputshaper-check.service, /usr/bin/systemctl start --no-block inputshaper-update.service
EOF
chmod 440 /etc/sudoers.d/inputshaper
visudo -cf /etc/sudoers.d/inputshaper
systemctl daemon-reload
python3 /usr/local/lib/inputshaper/update.py install
systemctl enable inputshaper.service inputshaper-check.timer
systemctl start inputshaper-check.timer
echo 'Input Shaper Lab is listening on port 8080. Password is in /etc/inputshaper.env.'
