FROM certbot/certbot:latest

# Install the Cloudflare DNS plugin for DNS-01 challenge
RUN pip install --no-cache-dir certbot-dns-cloudflare

# Install Docker CLI so the renewal script can reload nginx after renewal
RUN apk add --no-cache docker-cli

COPY renew.sh /renew.sh
RUN chmod +x /renew.sh

ENTRYPOINT ["/renew.sh"]
