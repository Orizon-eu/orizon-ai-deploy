"""Per-deployment Compose overrides: services moved off the release's image,
and services the release's compose files do not define at all.

Every Orizon image in the compose files is `<prefix>/<name>:${ORIZON_VERSION}`,
so the release moves them together, and the set of services is whatever the
release's compose files list. Two commands step outside that:

  `orizonctl update backend --version X`    one service on another image
  `orizonctl service add mcp-imap ...`      a container the files do not have

For either to survive the next `up`, the choice is recorded in the state
(`image_pins`, `mcp_image_pins`, `extra_services`) and rendered here into a
Compose override file per stack, which `Stack` adds after the main file:

    <app>/.orizon/overrides.yml        services of docker-compose.yml
    <app>/.orizon/overrides.mcp.yml    services of docker-compose.mcp.yml

The files are derived: `sync` rewrites them from the state and removes them
when there is nothing to say, so restoring state.json (rollback) is enough.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .state import STATE_DIR, State

PIN_FILE = "overrides.yml"
MCP_PIN_FILE = "overrides.mcp.yml"

HEADER = (
    "# Written by orizonctl from .orizon/state.json; do not edit.\n"
    "# `orizonctl update <service> --unpin` returns a service to ORIZON_VERSION;\n"
    "# `orizonctl service remove <service>` takes an added one out.\n"
)

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def path(app_dir: Path, mcp: bool = False) -> Path:
    return app_dir / STATE_DIR / (MCP_PIN_FILE if mcp else PIN_FILE)


def valid_ref(ref: str) -> bool:
    """An image reference Compose takes literally: no interpolation, no spaces."""
    return bool(ref) and "$" not in ref and not any(c.isspace() for c in ref) and not ref.startswith("-")


def valid_name(name: str) -> bool:
    return bool(_NAME.match(name))


def _q(value: object) -> str:
    """A YAML scalar Compose reads literally: a JSON string is a valid YAML
    double-quoted one, and `$$` is Compose's escape for a dollar sign."""
    return json.dumps(str(value)).replace("$", "$$")


def render_service(name: str, spec: dict) -> str:
    """The service block of an added container. One on the MCP stack gets what
    every connector in docker-compose.mcp.yml has: .env.mcp, MCP_PORT,
    MCP_CRED_VARS, the port healthcheck and the memory limits."""
    mcp = bool(spec.get("mcp"))
    port = spec.get("port")
    env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
    if mcp:
        env.setdefault("MCP_CRED_VARS", str(spec.get("cred_vars") or ""))
        if port:
            env.setdefault("MCP_PORT", str(port))
    out = [f"  {name}:\n", f"    image: {_q(spec['image'])}\n", f"    container_name: {_q('orizon-' + name)}\n"]
    if mcp:
        out.append("    env_file:\n      - .env.mcp\n")
    if env:
        out.append("    environment:\n")
        out += [f"      {k}: {_q(v)}\n" for k, v in sorted(env.items())]
    if port and spec.get("publish"):
        out.append(f"    ports:\n      - {_q(f'{port}:{port}')}\n")
    out.append("    networks:\n      - orizon-network\n    restart: unless-stopped\n")
    if mcp and port:
        check = f"import socket; socket.create_connection(('localhost', {int(port)}), timeout=5).close()"
        out.append(f"    healthcheck:\n      test: [\"CMD\", \"python3\", \"-c\", {_q(check)}]\n      interval: 30s\n      timeout: 10s\n      start_period: 15s\n      retries: 3\n")
    if mcp:
        out.append("    deploy:\n      resources:\n        limits:\n          memory: 256M\n          cpus: \"0.5\"\n")
    return "".join(out)


def render(pins: dict[str, str], extras: dict[str, dict] | None = None) -> str:
    lines = [HEADER, "services:\n"]
    for service in sorted(pins):
        lines.append(f"  {service}:\n    image: {_q(pins[service])}\n")
    for service in sorted(extras or {}):
        lines.append(render_service(service, extras[service]))
    return "".join(lines)


def sync(app_dir: Path, state: State | None) -> None:
    """Make the override files say what the state says."""
    for mcp in (False, True):
        pins = (state.mcp_image_pins if mcp else state.image_pins) if state else {}
        extras = {n: s for n, s in (state.extra_services if state else {}).items() if bool(s.get("mcp")) == mcp and s.get("image")}
        p = path(app_dir, mcp)
        if not pins and not extras:
            p.unlink(missing_ok=True)
            continue
        text = render(pins, extras)
        if p.exists() and p.read_text(encoding="utf-8") == text:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def tag_of(ref: str) -> str:
    """`registry:5000/a/b:v1` -> `v1`; `a/b@sha256:..` -> `sha256:..`; none -> `latest`."""
    if "@" in ref:
        return ref.rsplit("@", 1)[1]
    last = ref.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else "latest"


def retag(ref: str, tag: str) -> str:
    """The same repository at another tag."""
    repo = ref.rsplit("@", 1)[0] if "@" in ref else ref
    head, _, last = repo.rpartition("/")
    last = last.rsplit(":", 1)[0] if ":" in last else last
    return (f"{head}/" if head else "") + f"{last}:{tag}"
