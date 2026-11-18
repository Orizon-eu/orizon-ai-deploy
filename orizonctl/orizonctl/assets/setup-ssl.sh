#!/bin/bash
# ============================================================================
# setup-ssl.sh — Obtain SSL certificate and switch nginx to HTTPS
#
# Uses DNS-01 challenge via Cloudflare API — server does NOT need to be
# reachable from the internet.
#
# Prerequisites:
#   1. Your domain's nameservers point to Cloudflare
#   2. A record in Cloudflare points to your server IP
#   3. NGINX_DOMAIN, CERTBOT_EMAIL, CLOUDFLARE_API_TOKEN set in .env.nginx
#
# Usage: ./setup-ssl.sh
# ============================================================================

set -euo pipefail

# Two files. Everything nginx and certbot are launched with is in .env.nginx;
# NGINX_TEMPLATE stays in .env because Compose reads it as a bind mount path,
# which is also why this script writes the switch back there rather than here.
if [ ! -f .env.nginx ]; then
    echo "ERROR: .env.nginx not found. Copy env.example.nginx to .env.nginx and fill it in."
    exit 1
fi
if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy env.example to .env and fill in the required values."
    exit 1
fi

# Safely extract only the variables we need (avoids bash syntax errors from
# JSON values like FRONTEND_URLS=[...] when sourcing a whole env file)
_get_env() { grep -E "^$1=" .env.nginx | head -1 | cut -d'=' -f2- | tr -d '"'; }
NGINX_DOMAIN="${NGINX_DOMAIN:-$(_get_env NGINX_DOMAIN)}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-$(_get_env CERTBOT_EMAIL)}"
CERTBOT_STAGING="${CERTBOT_STAGING:-$(_get_env CERTBOT_STAGING)}"
CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-$(_get_env CLOUDFLARE_API_TOKEN)}"
CERTBOT_FORCE_RECREATE="${CERTBOT_FORCE_RECREATE:-$(_get_env CERTBOT_FORCE_RECREATE)}"

if [ -z "${NGINX_DOMAIN:-}" ]; then
    echo "ERROR: NGINX_DOMAIN is not set in .env.nginx"
    exit 1
fi

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    echo "ERROR: CLOUDFLARE_API_TOKEN is not set in .env.nginx"
    echo "       Get it from: Cloudflare → My Profile → API Tokens → Create Token"
    exit 1
fi

# Build staging flag
STAGING_FLAG=""
STAGING_LABEL=""
if [ "${CERTBOT_STAGING:-true}" = "true" ]; then
    STAGING_FLAG="--staging"
    STAGING_LABEL=" [STAGING — fake cert]"
fi

echo "================================================"
echo " SSL Setup for: $NGINX_DOMAIN${STAGING_LABEL}"
echo "================================================"

# Step 1: Verify DNS resolution
echo ""
echo "[1/5] Checking DNS resolution..."
if command -v dig >/dev/null 2>&1; then
    RESOLVED_IP=$(dig +short "$NGINX_DOMAIN" | grep -E '^[0-9]+\.' | head -1)
elif command -v nslookup >/dev/null 2>&1; then
    RESOLVED_IP=$(nslookup "$NGINX_DOMAIN" 2>/dev/null | awk '/^Address: [0-9]/ { print $2 }' | head -1)
else
    RESOLVED_IP=$(getent hosts "$NGINX_DOMAIN" 2>/dev/null | awk '{ print $1 }' | head -1)
fi
if [ -z "$RESOLVED_IP" ]; then
    echo "ERROR: $NGINX_DOMAIN does not resolve to any IP."
    echo "       Make sure your A record is in Cloudflare and nameservers have propagated."
    echo "       Check with: dig $NGINX_DOMAIN +short  (or: nslookup $NGINX_DOMAIN)"
    exit 1
fi
echo "  $NGINX_DOMAIN resolves to $RESOLVED_IP"

# Step 2: Build certbot image (ensures Cloudflare plugin is installed)
echo ""
echo "[2/5] Building certbot image (ensures Cloudflare plugin is up to date)..."
docker compose build certbot
echo "  certbot image ready"

# Step 3: Verify Cloudflare plugin is available in the image
echo ""
echo "[3/5] Verifying Cloudflare DNS plugin..."
PLUGIN_CHECK=$(docker compose run --rm --no-deps --entrypoint sh certbot \
    -c "certbot plugins 2>/dev/null | grep dns-cloudflare || echo missing")
if echo "$PLUGIN_CHECK" | grep -q "missing"; then
    echo "ERROR: certbot-dns-cloudflare plugin not found in image."
    echo "       Try: docker compose build --no-cache certbot"
    exit 1
fi
echo "  dns-cloudflare plugin confirmed"

# Step 4: Ensure nginx is running
echo ""
echo "[4/5] Ensuring nginx is running..."
if ! docker ps --format '{{.Names}}' | grep -q orizon-nginx; then
    echo "  nginx is not running. Starting it..."
    docker compose up -d nginx
    sleep 3
fi
echo "  nginx is running"

# Step 5: Request the certificate
echo ""
echo "[5/5] Checking for existing certificate..."
CERT_EXISTS=$(docker compose run --rm --no-deps --entrypoint sh certbot \
    -c "[ -f /etc/letsencrypt/live/$NGINX_DOMAIN/fullchain.pem ] && echo yes || echo no" 2>/dev/null | tr -d '[:space:]')

if [ "$CERT_EXISTS" = "yes" ]; then
    if [ "${CERTBOT_FORCE_RECREATE:-false}" = "true" ]; then
        echo "  CERTBOT_FORCE_RECREATE=true — deleting existing certificate to re-issue..."
        docker compose run --rm --no-deps --entrypoint sh certbot \
            -c "certbot delete --cert-name $NGINX_DOMAIN --non-interactive"
        CERT_EXISTS="no"
    else
        IS_STAGING_CERT=$(docker compose run --rm --no-deps --entrypoint sh certbot \
            -c "openssl x509 -in /etc/letsencrypt/live/$NGINX_DOMAIN/fullchain.pem -issuer -noout 2>/dev/null | grep -i 'fake\|staging' || true" \
            2>/dev/null | tr -d '[:space:]')
        if [ -n "$IS_STAGING_CERT" ]; then
            echo "  Staging certificate already exists — skipping issuance (reusing existing staging cert)."
        else
            echo "  Existing certificate detected (production or local) — skipping issuance."
        fi
        echo "  Set CERTBOT_FORCE_RECREATE=true to delete it and issue a new one."
    fi
fi

if [ "$CERT_EXISTS" = "no" ]; then
    echo "  No certificate found — requesting from Let's Encrypt via Cloudflare DNS${STAGING_LABEL}..."
    echo "  (this may take ~30 seconds for DNS propagation)"
    docker compose run --rm --no-deps --entrypoint sh certbot -c "
        mkdir -p /etc/letsencrypt/credentials &&
        printf 'dns_cloudflare_api_token = %s\n' '${CLOUDFLARE_API_TOKEN}' > /etc/letsencrypt/credentials/cloudflare.ini &&
        chmod 600 /etc/letsencrypt/credentials/cloudflare.ini &&
        certbot certonly \
            --dns-cloudflare \
            --dns-cloudflare-credentials /etc/letsencrypt/credentials/cloudflare.ini \
            --dns-cloudflare-propagation-seconds 30 \
            --email '${CERTBOT_EMAIL:-team@orizon.ai}' \
            --domain '${NGINX_DOMAIN}' \
            --agree-tos \
            --no-eff-email \
            --non-interactive \
            ${STAGING_FLAG}
    "
    echo "  Certificate obtained successfully!"
fi

# Switch to HTTPS template
echo ""
echo "Switching nginx to HTTPS template..."
sed -i 's|^NGINX_TEMPLATE=.*|NGINX_TEMPLATE=orizonai.conf.template|' .env
echo "  Updated .env: NGINX_TEMPLATE=orizonai.conf.template"

docker compose up -d nginx
echo "  nginx restarted with HTTPS"

docker compose up -d certbot
echo "  certbot renewal service started (checks every 12 hours)"

echo ""
echo "================================================"
echo " SSL setup complete!${STAGING_LABEL}"
echo " Your app is now available at: https://$NGINX_DOMAIN"
if [ -n "$STAGING_FLAG" ]; then
    echo ""
    echo " NOTE: This is a STAGING (fake) certificate."
    echo " Your browser will show a security warning — that's expected."
    echo " When ready for a real certificate:"
    echo "   1. Set CERTBOT_STAGING=false in .env.nginx"
    echo "   2. Re-run: ./setup-ssl.sh  (staging cert is deleted automatically)"
fi
echo ""
echo " IMPORTANT: Use 'docker compose down' to stop — NOT 'docker compose down -v'"
echo " The -v flag deletes volumes including your certificate."
echo "================================================"
