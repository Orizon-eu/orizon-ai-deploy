#!/bin/sh
# Certbot renewal loop using DNS-01 (Cloudflare) challenge.
# Server does NOT need to be reachable from the internet.
#
# On every startup:
#   1. If no certificate exists → issue one (unless STAGING_FLAG would overwrite a production cert)
#   2. Run certbot renew immediately (handles expired/near-expiry certs)
#   3. Sleep 12 hours, then repeat from step 2
#
# Required environment variables (passed from docker-compose):
#   NGINX_DOMAIN             — e.g. yourdomain.com
#   CERTBOT_EMAIL            — contact email for Let's Encrypt account
#   CLOUDFLARE_API_TOKEN     — Cloudflare API token with Edit zone DNS permission
#   CERTBOT_STAGING          — "true" = use Let's Encrypt staging (fake certs, no rate limits)
#   NGINX_CONTAINER_NAME     — name of the nginx container (default: orizon-nginx)
#   CERTBOT_FORCE_RECREATE   — "true" = delete existing cert and re-issue (ignores staging protection)

set -e

NGINX_CONTAINER="${NGINX_CONTAINER_NAME:-orizon-nginx}"
CREDS_FILE="/etc/letsencrypt/credentials/cloudflare.ini"
CERT_FILE="/etc/letsencrypt/live/$NGINX_DOMAIN/fullchain.pem"
SLEEP_SECONDS=43200  # 12 hours

# Build staging flag
STAGING_FLAG=""
if [ "${CERTBOT_STAGING:-true}" = "true" ]; then
    STAGING_FLAG="--staging"
    echo "[certbot] *** STAGING MODE — certificates will be FAKE (untrusted) ***"
fi

# Write Cloudflare credentials file on every startup so the token stays
# up to date if it was rotated.
mkdir -p "$(dirname "$CREDS_FILE")"
printf 'dns_cloudflare_api_token = %s\n' "$CLOUDFLARE_API_TOKEN" > "$CREDS_FILE"
chmod 600 "$CREDS_FILE"

echo "[certbot] Starting for domain: $NGINX_DOMAIN"

# Helper: returns 0 if the existing cert is a production (non-staging) cert
_is_production_cert() {
    openssl x509 -in "$CERT_FILE" -issuer -noout 2>/dev/null \
        | grep -qi "fake\|staging" && return 1 || return 0
}

# ── FORCE_RECREATE: delete existing cert so a fresh one is issued below ──
if [ "${CERTBOT_FORCE_RECREATE:-false}" = "true" ]; then
    if [ -f "$CERT_FILE" ]; then
        echo "[certbot] CERTBOT_FORCE_RECREATE=true — deleting existing certificate for $NGINX_DOMAIN..."
        certbot delete --cert-name "$NGINX_DOMAIN" --non-interactive 2>/dev/null \
            && echo "[certbot] Existing certificate deleted." \
            || echo "[certbot] WARNING: certbot delete failed — continuing anyway."
    else
        echo "[certbot] CERTBOT_FORCE_RECREATE=true — no existing certificate to delete."
    fi
fi

# ── Step 1: Issue certificate if none exists yet ──
if [ ! -f "$CERT_FILE" ]; then
    echo "[certbot] No certificate found — requesting initial certificate..."

    if certbot certonly \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$CREDS_FILE" \
        --dns-cloudflare-propagation-seconds 30 \
        --email "$CERTBOT_EMAIL" \
        --domain "$NGINX_DOMAIN" \
        --agree-tos \
        --no-eff-email \
        --non-interactive \
        $STAGING_FLAG; then
        echo "[certbot] Certificate obtained successfully!"
        # Reload nginx to pick up the new certificate
        docker exec "$NGINX_CONTAINER" nginx -s reload 2>/dev/null \
            && echo "[certbot] nginx reloaded with new certificate" \
            || echo "[certbot] nginx reload failed (container may not be running)"
    else
        echo "[certbot] ERROR: Certificate request failed. Check CLOUDFLARE_API_TOKEN and domain."
    fi
else
    # Cert already exists — never overwrite (production, staging, or local).
    # Only CERTBOT_FORCE_RECREATE=true (handled above) can replace it.
    if _is_production_cert; then
        echo "[certbot] Production certificate already exists — skipping issuance."
    else
        echo "[certbot] Staging/local certificate already exists — skipping issuance (reusing existing cert)."
    fi
    echo "[certbot] Set CERTBOT_FORCE_RECREATE=true to delete it and issue a new one."
fi

# ── Step 2+: Renewal loop — runs immediately on startup, then every 12 hours ──
# certbot renew checks local cert expiry first — it only contacts Let's Encrypt
# when expiry is within 30 days. No rate-limit impact from 12-hour polling.
# --deploy-hook fires only when a cert is actually renewed, so nginx is NOT
# reloaded on no-op checks.
while true; do
    echo "[certbot] $(date) — checking certificate renewal..."

    certbot renew \
        --dns-cloudflare \
        --dns-cloudflare-credentials "$CREDS_FILE" \
        --dns-cloudflare-propagation-seconds 30 \
        --non-interactive \
        --quiet \
        --deploy-hook "docker exec $NGINX_CONTAINER nginx -s reload" \
    && echo "[certbot] Renewal check done (renewed if expiry was within 30 days)" \
    || echo "[certbot] Renewal failed — check logs"

    echo "[certbot] Next check in 12 hours..."
    sleep "$SLEEP_SECONDS"
done
