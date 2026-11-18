"""Snapshots: everything an operator needs to put a deployment back the way
it was, in one directory under `$ORIZON_HOME/backups/`.

    <ts>_<kind>_<version>/
      manifest.json      what, when, from which version to which, what is inside
      db.dump            pg_dump -Fc of the application database
      env/               the .env* files, .orizon/state.json, .orizon/assets.json
      files.tar.gz       the deployment files (compose files, templates, nginx,
                         helper scripts) as they were: the previous release's copy
      data.tar.gz        the bind-mounted client data (uploads, indexes, bot
                         sessions, custom certificates); logs are not included
      letsencrypt/       the certificate volume, when nginx was running

`update` takes one before it changes anything, so `rollback` has a complete
previous state to return to; `backup` takes the same on demand.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, ui
from .assets import bundled_files
from .compose import ComposeError, Stack
from .schema import EnvFile
from .state import State

MANIFEST = "manifest.json"
DB_DUMP = "db.dump"
ENV_DIR = "env"
FILES_TAR = "files.tar.gz"
DATA_TAR = "data.tar.gz"
LETSENCRYPT_DIR = "letsencrypt"

# Bind mounts holding client data (relative to the app directory). Logs are
# left out on purpose: large, and nothing a rollback needs.
DATA_DIRS = ("backend-orizon-ai/data", "transcription-bot-orizon-ai/data", "certs")
STATE_FILES = ("state.json", "assets.json")


@dataclass
class Manifest:
    name: str
    kind: str  # manual | update
    created_at: str
    from_version: str
    to_version: str = ""
    image_prefix: str = ""
    orizonctl_version: str = __version__
    cli_source: str = ""
    app_dir: str = ""
    contents: dict = field(default_factory=dict)  # part -> detail (size, files)
    status: str = "created"  # created | applied (the update it preceded went through) | rolled-back

    @classmethod
    def load(cls, path: Path) -> "Manifest | None":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        try:
            return cls(**known)
        except TypeError:
            return None

    def save(self, directory: Path) -> None:
        (directory / MANIFEST).write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


@dataclass
class Snapshot:
    dir: Path
    manifest: Manifest

    @property
    def name(self) -> str:
        return self.dir.name

    @property
    def kind_is_update(self) -> bool:
        return self.manifest.kind == "update"

    def has(self, part: str) -> bool:
        return (self.dir / part).exists()

    def save(self) -> None:
        self.manifest.save(self.dir)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ----------------------------------------------------------------- create


def deployment_files(app_dir: Path) -> list[str]:
    """The release-specific files under the app directory: whatever the bundle
    lists, plus the compose files and templates of a checkout that predates
    the bundle record."""
    names = set(bundled_files())
    names.update(["docker-compose.yml", "docker-compose.mcp.yml", "env.example", "env.example.secrets", "env.example.vllm", "env.example.mcp", "env.example.nginx"])
    for p in (app_dir / "nginx").glob("*.template"):
        names.add(f"nginx/{p.name}")
    return sorted(n for n in names if (app_dir / n).is_file())


def dump_database(stack: Stack, values: dict[str, str], dest: Path) -> int:
    user = values.get("POSTGRES_USER", "postgres")
    db = values.get("POSTGRES_DB", "orizon")
    if stack.service_status("db") != "ready":
        raise ComposeError("the database container is not running; start it with `orizonctl up` or `docker compose up -d db`")
    with open(dest, "wb") as fh:
        r = stack.exec("db", ["pg_dump", "-U", user, "-d", db, "-Fc"], capture=False, stdout=fh, timeout=3600)
    if r.returncode != 0:
        dest.unlink(missing_ok=True)
        err = (r.stderr or b"").decode(errors="replace").strip() if r.stderr else ""
        raise ComposeError("pg_dump failed" + (f": {err}" if err else ""))
    os.chmod(dest, 0o600)
    return dest.stat().st_size


def create_snapshot(
    home: Path,
    app_dir: Path,
    stack: Stack,
    values: dict[str, str],
    state: State | None,
    kind: str = "manual",
    to_version: str = "",
    include_db: bool = True,
    include_data: bool = True,
    cli_source: str = "",
) -> Snapshot:
    version = (state.version if state else "") or values.get("ORIZON_VERSION", "") or "unknown"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = home / "backups" / f"{ts}_{kind}_{version}"
    n = 1
    while directory.exists():  # two snapshots in one second (tests, scripted runs)
        n += 1
        directory = directory.with_name(f"{ts}_{kind}_{version}-{n}")
    directory.mkdir(parents=True, exist_ok=False)
    os.chmod(directory, 0o700)
    man = Manifest(
        name=directory.name,
        kind=kind,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        from_version=version,
        to_version=to_version,
        image_prefix=(state.image_prefix if state else "") or values.get("ORIZON_IMAGE_PREFIX", ""),
        cli_source=cli_source,
        app_dir=str(app_dir),
    )
    snap = Snapshot(directory, man)

    # env files + state: small, always
    env_dir = directory / ENV_DIR
    env_dir.mkdir()
    copied = []
    for f in EnvFile:
        src = app_dir / f.value
        if src.exists():
            shutil.copy2(src, env_dir / f.value)
            copied.append(f.value)
    for name in STATE_FILES:
        src = app_dir / ".orizon" / name
        if src.exists():
            shutil.copy2(src, env_dir / name)
            copied.append(name)
    for p in env_dir.iterdir():
        os.chmod(p, 0o600)
    man.contents["env"] = {"files": copied}
    ui.ok(f"env files + state: {', '.join(copied) or 'none'}")

    # deployment files of this release
    files = deployment_files(app_dir)
    with tarfile.open(directory / FILES_TAR, "w:gz") as tar:
        for rel in files:
            tar.add(app_dir / rel, arcname=rel)
    os.chmod(directory / FILES_TAR, 0o600)
    man.contents["files"] = {"count": len(files), "size": _size(directory / FILES_TAR)}
    ui.ok(f"deployment files: {len(files)} files")

    # database
    if include_db:
        ui.step("pg_dump")
        size = dump_database(stack, values, directory / DB_DUMP)
        man.contents["db"] = {"size": size, "database": values.get("POSTGRES_DB", "orizon"), "user": values.get("POSTGRES_USER", "postgres")}
        ui.ok(f"database dump: {human(size)}")

    # client data
    if include_data:
        present = [d for d in DATA_DIRS if (app_dir / d).is_dir()]
        total = sum(_size(app_dir / d) for d in present)
        if total > 20 * 1024**3:
            ui.warn(f"client data is {human(total)}; the archive will take a while (`--no-data` skips it)")
        ui.step(f"archiving client data ({human(total)}): {', '.join(present) or 'nothing present'}")
        with tarfile.open(directory / DATA_TAR, "w:gz") as tar:
            for d in present:
                tar.add(app_dir / d, arcname=d)
        os.chmod(directory / DATA_TAR, 0o600)
        man.contents["data"] = {"dirs": present, "size": _size(directory / DATA_TAR), "raw_size": total}
        ui.ok(f"client data: {human(_size(directory / DATA_TAR))} archived")

    # the certificate volume, through the running nginx container
    if state is not None and state.access_mode == "letsencrypt":
        ids = stack.container_ids("nginx")
        if ids and stack.copy_from_container(ids[0], "/etc/letsencrypt", directory / LETSENCRYPT_DIR):
            man.contents["letsencrypt"] = {"size": _size(directory / LETSENCRYPT_DIR)}
            ui.ok("Let's Encrypt certificates copied")
        else:
            ui.info("nginx is not running; the certificate volume was not copied (a rollback re-issues with `orizonctl ssl issue`)")

    snap.save()
    ui.ok(f"snapshot {directory.name} ({human(_size(directory))})")
    return snap


# ------------------------------------------------------------------- list


def list_snapshots(home: Path) -> list[Snapshot]:
    root = home / "backups"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / MANIFEST).exists():
            man = Manifest.load(d / MANIFEST)
            if man:
                out.append(Snapshot(d, man))
    return out


def find_snapshot(home: Path, name: str | None) -> Snapshot | None:
    snaps = list_snapshots(home)
    if name:
        p = Path(name)
        if p.is_dir() and (p / MANIFEST).exists():
            man = Manifest.load(p / MANIFEST)
            return Snapshot(p, man) if man else None
        for s in snaps:
            if s.name == name:
                return s
        return None
    updates = [s for s in snaps if s.kind_is_update]
    return (updates or snaps)[-1] if (updates or snaps) else None


def prune(home: Path, keep: int) -> list[Snapshot]:
    snaps = list_snapshots(home)
    doomed = snaps[:-keep] if keep > 0 else snaps
    for s in doomed:
        shutil.rmtree(s.dir, ignore_errors=True)
    return doomed


# ---------------------------------------------------------------- restore


def restore_env(snap: Snapshot, app_dir: Path) -> list[str]:
    src = snap.dir / ENV_DIR
    restored = []
    for f in EnvFile:
        p = src / f.value
        if p.exists():
            shutil.copy2(p, app_dir / f.value)
            restored.append(f.value)
    (app_dir / ".orizon").mkdir(parents=True, exist_ok=True)
    for name in STATE_FILES:
        p = src / name
        if p.exists():
            shutil.copy2(p, app_dir / ".orizon" / name)
            restored.append(name)
    return restored


def restore_files(snap: Snapshot, app_dir: Path) -> int:
    tarp = snap.dir / FILES_TAR
    if not tarp.exists():
        return 0
    n = 0
    with tarfile.open(tarp, "r:gz") as tar:
        for m in tar.getmembers():
            if not _safe_member(m):
                continue
            tar.extract(m, app_dir, **_EXTRACT_KW)
            n += 1
    return n


def restore_data(snap: Snapshot, app_dir: Path) -> list[str]:
    """Extract the data archive over the current directories. Files created
    after the snapshot stay in place; files the snapshot holds are put back."""
    tarp = snap.dir / DATA_TAR
    if not tarp.exists():
        return []
    dirs = (snap.manifest.contents.get("data") or {}).get("dirs") or []
    with tarfile.open(tarp, "r:gz") as tar:
        members = [m for m in tar.getmembers() if _safe_member(m)]
        tar.extractall(app_dir, members=members, **_EXTRACT_KW)
    return dirs


def restore_database(snap: Snapshot, stack: Stack, values: dict[str, str]) -> None:
    dump = snap.dir / DB_DUMP
    if not dump.exists():
        raise ComposeError(f"{snap.name} holds no database dump")
    info = snap.manifest.contents.get("db") or {}
    user = info.get("user") or values.get("POSTGRES_USER", "postgres")
    db = info.get("database") or values.get("POSTGRES_DB", "orizon")
    if stack.service_status("db") != "ready":
        raise ComposeError("the database container is not ready")
    # Close every other session first: a connected backend would block DROP.
    stack.exec("db", ["psql", "-U", user, "-d", "postgres", "-qc", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db}' AND pid <> pg_backend_pid();"], timeout=60)
    with open(dump, "rb") as fh:
        r = stack.exec("db", ["pg_restore", "-U", user, "-d", db, "--clean", "--if-exists", "--no-owner", "--exit-on-error"], capture=True, stdin_file=fh, timeout=3600)
    if r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace").strip()
        raise ComposeError(f"pg_restore failed: {err[-1500:]}")


def restore_letsencrypt(snap: Snapshot, stack: Stack) -> bool:
    src = snap.dir / LETSENCRYPT_DIR
    if not src.is_dir():
        return False
    # certbot mounts the volume read-write; nginx has it read-only
    for service in ("certbot", "nginx"):
        ids = stack.container_ids(service)
        if ids and stack.copy_to_container(src, ids[0], "/etc/letsencrypt"):
            return True
    return False


# Python 3.12+ can refuse absolute paths and links itself; older versions rely on _safe_member alone.
_EXTRACT_KW = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}


def _safe_member(m: tarfile.TarInfo) -> bool:
    name = m.name
    return not (name.startswith("/") or ".." in Path(name).parts or m.issym() or m.islnk())
