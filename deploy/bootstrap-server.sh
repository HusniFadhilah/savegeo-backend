#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/home/ubuntu/savegeo
BACKEND_DIR="$APP_ROOT/backend"
FRONTEND_DIR="$APP_ROOT/frontend"

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  apache2 docker.io docker-compose-v2 unzip curl

sudo systemctl enable --now docker apache2
sudo usermod -aG docker ubuntu

mkdir -p \
  "$BACKEND_DIR/var/uploads" \
  "$BACKEND_DIR/var/saved_models" \
  "$BACKEND_DIR/var/gee-credentials" \
  "$BACKEND_DIR/var/exports" \
  "$FRONTEND_DIR/dist"

if [[ ! -f "$BACKEND_DIR/.env" ]]; then
  cp "$BACKEND_DIR/.env.production.example" "$BACKEND_DIR/.env"
fi

chmod 600 "$BACKEND_DIR/.env"

if ! grep -Eq '^POSTGRES_PASSWORD=.+$' "$BACKEND_DIR/.env"; then
  postgres_password="$(openssl rand -hex 32)"
  if grep -q '^POSTGRES_PASSWORD=' "$BACKEND_DIR/.env"; then
    sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$postgres_password/" "$BACKEND_DIR/.env"
  else
    printf '\nPOSTGRES_PASSWORD=%s\n' "$postgres_password" >> "$BACKEND_DIR/.env"
  fi
fi

if grep -q '^JWT_SECRET_KEY=change_' "$BACKEND_DIR/.env"; then
  jwt_secret="$(openssl rand -hex 48)"
  sed -i "s/^JWT_SECRET_KEY=.*/JWT_SECRET_KEY=$jwt_secret/" "$BACKEND_DIR/.env"
fi

sudo a2enmod proxy proxy_http headers rewrite
apache_config="$FRONTEND_DIR/deploy/savegeo-apache.conf"
[[ -f "$apache_config" ]] || apache_config="$FRONTEND_DIR/savegeo-apache.conf"
sudo install -m 0644 "$apache_config" \
  /etc/apache2/sites-available/savegeo.len.co.id.conf
sudo a2dissite savegeo.len.co.id 2>/dev/null || true
sudo a2ensite savegeo.len.co.id.conf
sudo apache2ctl configtest
sudo systemctl reload apache2

echo "Bootstrap complete. Log out and back in once so the docker group takes effect."
