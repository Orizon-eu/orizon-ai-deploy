"""The deployment files the CLI carries, and how they land on the host.

`orizonctl` is installed from a public package; the application source stays
private. So the compose files, env templates, nginx templates and helper
scripts a host needs travel inside the package (`orizonctl/assets/`, kept in
step with the monorepo by `sync-assets.sh`) and are written out to the app
directory by `materialize()`.

Local edits are respected: every file written is recorded with its hash in
`.orizon/assets.json`. On the next materialize (an upgrade), a file whose
content still matches what we wrote is replaced; one the operator changed is
kept and reported, unless `force` is given, in which case the operator's copy
is saved under `.orizon/local-edits/<timestamp>/` first.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

MANIFEST_NAME = "MANIFEST"
RECORD = Path(".orizon") / "assets.json"
EXECUTABLE_SUFFIXES = (".sh",)


def _root():
    return resources.files("orizonctl") / "assets"


def bundled_files() -> list[str]:
    """Relative paths of every bundled asset, in MANIFEST order."""
    root = _root()
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        return []
    return [l.strip() for l in manifest.read_text(encoding="utf-8").splitlines() if l.strip()]


def read_bundled(rel: str) -> bytes:
    return (_root() / rel).read_bytes()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_bundle_available() -> bool:
    return bool(bundled_files())


@dataclass
class MaterializeReport:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept_local: list[str] = field(default_factory=list)
    overwritten_local: list[str] = field(default_factory=list)
    backup_dir: Path | None = None


def _load_record(app_dir: Path) -> dict[str, str]:
    p = app_dir / RECORD
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_record(app_dir: Path, record: dict[str, str]) -> None:
    p = app_dir / RECORD
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def materialize(app_dir: Path, force: bool = False) -> MaterializeReport:
    report = MaterializeReport()
    record = _load_record(app_dir)
    new_record: dict[str, str] = dict(record)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for rel in bundled_files():
        data = read_bundled(rel)
        digest = sha256(data)
        dest = app_dir / rel
        if dest.exists():
            current = sha256(dest.read_bytes())
            if current == digest:
                report.unchanged.append(rel)
                new_record[rel] = digest
                continue
            if rel in record and record[rel] != current:
                # the operator edited this file since we wrote it
                if not force:
                    report.kept_local.append(rel)
                    continue
                backup = app_dir / ".orizon" / "local-edits" / stamp / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(dest.read_bytes())
                report.backup_dir = app_dir / ".orizon" / "local-edits" / stamp
                report.overwritten_local.append(rel)
            elif rel not in record and not force:
                # a file we never wrote (a dev checkout, or a hand-made copy)
                report.kept_local.append(rel)
                continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if rel.endswith(EXECUTABLE_SUFFIXES):
            os.chmod(dest, 0o755)
        new_record[rel] = digest
        report.written.append(rel)

    _save_record(app_dir, new_record)
    return report


def drifted(app_dir: Path) -> list[str]:
    """Bundled files whose copy in the app directory differs from this
    orizonctl's version: local edits, or an older file kept over an upgrade."""
    out = []
    for rel in bundled_files():
        dest = app_dir / rel
        if dest.is_file() and sha256(dest.read_bytes()) != sha256(read_bundled(rel)):
            out.append(rel)
    return out


def app_dir_is_materialized(app_dir: Path) -> bool:
    return (app_dir / "docker-compose.yml").exists() and (app_dir / "env.example").exists()
