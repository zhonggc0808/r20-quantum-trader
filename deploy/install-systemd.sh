#!/bin/sh
set -eu

# Install R20 into a service-readable path and render the two supported units.
# The repository may live under /root during development; systemd services must
# never point a non-root service user into /root.
SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
INSTALL_ROOT=${R20_INSTALL_ROOT:-/opt/r20-account-isolation}
SERVICE_USER=${R20_SERVICE_USER:-r20}
SERVICE_GROUP=${R20_SERVICE_GROUP:-$SERVICE_USER}

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run as root" >&2; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is required" >&2; exit 1; }

if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_GROUP" --home-dir "/home/$SERVICE_USER" \
    --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$INSTALL_ROOT"
rsync -a --delete \
  --exclude '.git/' --exclude '.venv/' --exclude 'frontend/node_modules/' \
  --exclude 'logs/' --exclude '.env' \
  "$SOURCE_ROOT/" "$INSTALL_ROOT/"

if [ ! -f "$INSTALL_ROOT/.env" ] && [ -f "$SOURCE_ROOT/.env" ]; then
  install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 "$SOURCE_ROOT/.env" "$INSTALL_ROOT/.env"
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_ROOT"
chmod 700 "$INSTALL_ROOT/data" "$INSTALL_ROOT/.env" 2>/dev/null || true

# The normal installer runs against the installed checkout, so all generated
# paths and the virtualenv agree with the systemd units.
(cd "$INSTALL_ROOT" && ./deploy/install.sh)

for unit in r20-quantum.service r20-gateway.service; do
  sed -e "s#^User=.*#User=$SERVICE_USER#" \
      -e "s#^Group=.*#Group=$SERVICE_GROUP#" \
      -e "s#^Environment=HOME=.*#Environment=HOME=/home/$SERVICE_USER#" \
      -e "s#^Environment=PATH=.*#Environment=PATH=$INSTALL_ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin#" \
      -e "s#^WorkingDirectory=.*#WorkingDirectory=$INSTALL_ROOT#" \
      -e "s#^EnvironmentFile=.*#EnvironmentFile=$INSTALL_ROOT/.env#" \
      -e "s#^ExecStart=.*python -m#ExecStart=$INSTALL_ROOT/.venv/bin/python -m#" \
      "$SOURCE_ROOT/deploy/$unit" > "/etc/systemd/system/$unit"
done

# r20-gateway owns the scheduler. The legacy scheduler is deliberately stopped
# and disabled to prevent duplicate trader/factor/news subprocesses.
systemctl disable --now r20-scheduler.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now r20-quantum.service r20-gateway.service

echo "Installed R20 services for $SERVICE_USER at $INSTALL_ROOT"
