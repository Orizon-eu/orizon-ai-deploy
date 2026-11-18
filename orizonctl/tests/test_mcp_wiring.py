"""The bundled env templates are edited by hand (BUNDLE_OWNED in sync-assets.sh),
while docker-compose.mcp.yml is synced from the monorepo. A connector added to
the compose file therefore has to be wired into three hand-kept places, or a
fresh install loads a connector the backend cannot reach:

  - `MCP_<X>_PORT=<n>` in env.example.mcp and `MCP_PORTS` in schema.py
  - `mcp-<name>:<n>` and `localhost:<n>` in the shared `MCP_ALLOWED_HOSTS`
  - `MCP_<X>_URL=http://mcp-<name>:<n>/mcp` in env.example

This reads the compose file as the source of truth and checks the other three.
"""

import re
from pathlib import Path

from orizonctl import envfile, schema

BUNDLE = Path(__file__).resolve().parents[1] / "orizonctl" / "assets"

# Connectors whose URL is not an env key: the sandbox's address is an admin
# setting (ORIZON_MCP_SANDBOX_URL in the backend's settings registry).
NO_URL_KEY = {"SANDBOX"}

_SERVICE = re.compile(r"^  (mcp-[a-z]+):$")
_PORT = re.compile(r"\$\{MCP_([A-Z]+)_PORT:-(\d+)\}")


def compose_connectors() -> dict[str, tuple[str, int]]:
    """{X: (service, port)} for every `mcp-<name>` service whose port is
    `${MCP_<X>_PORT:-<n>}` in the bundled compose file."""
    out: dict[str, tuple[str, int]] = {}
    service = None
    for line in (BUNDLE / "docker-compose.mcp.yml").read_text(encoding="utf-8").splitlines():
        m = _SERVICE.match(line)
        if m:
            service = m.group(1)
            continue
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            service = None  # a service that is not mcp-*
        m = _PORT.search(line)
        if m and service:
            out.setdefault(m.group(1), (service, int(m.group(2))))
    return out


def test_compose_lists_the_known_connectors():
    found = compose_connectors()
    assert {"HUBSPOT", "MICROSOFT", "GOOGLE", "IMAP", "SANDBOX"} <= set(found)
    assert found["IMAP"] == ("mcp-imap", 8111)


def test_every_connector_port_is_declared_once():
    found = compose_connectors()
    declared = dict(envfile.parse((BUNDLE / "env.example.mcp").read_text(encoding="utf-8")))
    for x, (service, port) in found.items():
        key = f"MCP_{x}_PORT"
        assert declared.get(key) == str(port), f"{key}={port} missing from env.example.mcp (service {service})"
        assert schema.MCP_PORTS.get(key) == port, f"{key}: schema.MCP_PORTS says {schema.MCP_PORTS.get(key)}, compose says {port}"
    stale = set(schema.MCP_PORTS) - {f"MCP_{x}_PORT" for x in found}
    assert not stale, f"schema.MCP_PORTS names ports no compose service uses: {sorted(stale)}"


def test_every_connector_is_on_the_host_allowlist():
    found = compose_connectors()
    declared = dict(envfile.parse((BUNDLE / "env.example.mcp").read_text(encoding="utf-8")))
    allowed = {h.strip() for h in declared["MCP_ALLOWED_HOSTS"].split(",")}
    for x, (service, port) in found.items():
        for host in (f"{service}:{port}", f"localhost:{port}"):
            assert host in allowed, f"{host} missing from MCP_ALLOWED_HOSTS (the backend's requests would get 421)"


def test_every_connector_has_its_url_key():
    found = compose_connectors()
    declared = dict(envfile.parse((BUNDLE / "env.example").read_text(encoding="utf-8")))
    for x, (service, port) in found.items():
        key = f"MCP_{x}_URL"
        if x in NO_URL_KEY:
            assert key not in declared, f"{key} is now in env.example; drop {x} from NO_URL_KEY"
            continue
        assert declared.get(key) == f"http://{service}:{port}/mcp", f"{key} missing or wrong in env.example"
    url_keys = {k[4:-4] for k in declared if k.startswith("MCP_") and k.endswith("_URL")}
    assert url_keys <= set(found), f"env.example names connectors with no compose service: {sorted(url_keys - set(found))}"
