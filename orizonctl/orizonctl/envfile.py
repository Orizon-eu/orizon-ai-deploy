"""Read and write Compose-style env files without losing their comments.

The repo ships one documented template per file (`env.example*`). We never
generate a file from scratch: we take the operator's existing file when there
is one, else the template, and set the keys the wizard owns in place. Every
comment and every key we do not touch survives, and a key that the template
only shows commented out (`# LLM_API_URL=`) is uncommented rather than
appended, so the generated file reads like the template it came from.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_ACTIVE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_COMMENTED = re.compile(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

MARKER = "# ---------------- set by orizonctl (keys not in the template) ----------------"


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text: str) -> dict[str, str]:
    """Active KEY=VALUE lines, quotes stripped. Later duplicates win, as in Compose."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _ACTIVE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = unquote(m.group(2))
    return out


def load(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse(path.read_text(encoding="utf-8"))


def quote_if_needed(value: str) -> str:
    """Compose strips an inline comment after ` #` from an unquoted value and
    expands `$`; quote when either would change what the container receives."""
    if value == "":
        return ""
    needs = " #" in value or value.startswith("#") or value != value.strip() or "\n" in value
    if needs:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def render(base: str, values: dict[str, str]) -> str:
    """Set every key in `values` inside `base`, keeping everything else.

    Rules, in order, per key:
      1. an active `KEY=` line is rewritten in place (all of them, if repeated);
      2. otherwise the first commented `# KEY=` line is uncommented and set;
      3. otherwise the key is appended under MARKER.
    """
    lines = base.splitlines()
    pending = dict(values)
    active_keys = {m.group(1) for m in (_ACTIVE.match(l) for l in lines if not l.lstrip().startswith("#")) if m}

    out: list[str] = []
    for line in lines:
        if line.lstrip().startswith("#"):
            m = _COMMENTED.match(line)
            if m and m.group(1) in pending and m.group(1) not in active_keys:
                key = m.group(1)
                out.append(f"{key}={quote_if_needed(pending.pop(key))}")
                active_keys.add(key)  # a second commented copy stays a comment
                continue
            out.append(line)
            continue
        m = _ACTIVE.match(line)
        if m and m.group(1) in values:
            key = m.group(1)
            out.append(f"{key}={quote_if_needed(values[key])}")
            pending.pop(key, None)
            continue
        out.append(line)

    if pending:
        if MARKER not in out:
            out.append("")
            out.append(MARKER)
        for key in pending:
            out.append(f"{key}={quote_if_needed(pending[key])}")
    return "\n".join(out) + "\n"


def drop(text: str, keys: set[str]) -> str:
    kept = [l for l in text.splitlines() if l.lstrip().startswith("#") or not (m := _ACTIVE.match(l)) or m.group(1) not in keys]
    return "\n".join(kept) + "\n"


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    """Write via a temp file in the same directory, then rename, so a crash
    mid-write never leaves a half-written .env for Compose to read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def missing_keys(template: str, current: str) -> list[str]:
    """Keys the template declares (active) that the operator's file lacks, for
    spotting a template that grew between versions."""
    have = parse(current)
    return [k for k in parse(template) if k not in have]
