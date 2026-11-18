"""Ask a container registry which tags exist, with the credentials `docker
login` already stored. Standard OCI distribution API: /v2/<repo>/tags/list,
with the bearer-token dance Harbor, GitLab and Docker Hub all use."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .schema import registry_host

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(.*)$")


def docker_credentials(host: str, config: Path | None = None) -> tuple[str, str] | None:
    """The username/password `docker login` stored for `host`, or None when
    there is none or a credential helper holds it (Docker Desktop)."""
    config = config or Path.home() / ".docker" / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    auths = data.get("auths") or {}
    keys = [host, f"https://{host}", f"https://{host}/v2/"]
    if host in ("docker.io", "registry-1.docker.io", "index.docker.io"):
        keys += ["https://index.docker.io/v1/", "index.docker.io", "docker.io"]
    for k in keys:
        entry = auths.get(k)
        if entry and entry.get("auth"):
            try:
                user, _, pw = base64.b64decode(entry["auth"]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            return user, pw
    return None


def _split(prefix: str, image: str) -> tuple[str, str]:
    """(api host, repository path) for prefix/image."""
    prefix = prefix.strip("/")
    host = registry_host(prefix)
    if host == "docker.io":
        repo = f"{prefix}/{image}" if "/" not in prefix else f"{prefix}/{image}"
        return "registry-1.docker.io", repo
    return host, f"{prefix[len(host):].strip('/')}/{image}"


def _get(url: str, headers: dict[str, str], timeout: int = 20) -> tuple[int, dict, bytes]:
    """Response headers come back lower-cased: registries differ in casing
    (Docker Hub sends `www-authenticate`, Harbor `Www-Authenticate`)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def list_tags(prefix: str, image: str, creds: tuple[str, str] | None = None, timeout: int = 20) -> list[str]:
    """Tags of prefix/image, or [] when the registry cannot be asked (no
    network, no permission, repository absent). Never raises."""
    host, repo = _split(prefix, image)
    url = f"https://{host}/v2/{repo}/tags/list?n=1000"
    creds = creds or docker_credentials(registry_host(prefix))
    basic = {"Authorization": "Basic " + base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()} if creds else {}
    try:
        status, headers, body = _get(url, basic, timeout)
        if status == 401:
            challenge = headers.get("www-authenticate", "")
            m = re.match(r'Bearer\s+(.*)', challenge, re.I)
            if not m:
                return []
            params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            realm = params.pop("realm", None)
            if not realm:
                return []
            params.setdefault("scope", f"repository:{repo}:pull")
            tstatus, _, tbody = _get(realm + "?" + urllib.parse.urlencode(params), basic, timeout)
            if tstatus != 200:
                return []
            token = json.loads(tbody).get("token") or json.loads(tbody).get("access_token")
            if not token:
                return []
            status, headers, body = _get(url, {"Authorization": f"Bearer {token}"}, timeout)
        if status != 200:
            return []
        tags = json.loads(body).get("tags") or []
        return sorted((t for t in tags if isinstance(t, str)), key=version_key)
    except Exception:
        return []


def version_key(tag: str):
    """Natural order for tags like v1.2.0, v1.2.0_dev, v1.10.3: numbers first,
    then a plain release above its suffixed variants (v1.2.0 > v1.2.0_dev)."""
    m = _VERSION_RE.match(tag)
    if not m:
        return (0, 0, 0, 0, 0, tag)
    major, minor, patch, suffix = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    return (1, major, minor, patch, 1 if suffix == "" else 0, suffix)


def newest(tags: list[str]) -> str | None:
    return max(tags, key=version_key) if tags else None
