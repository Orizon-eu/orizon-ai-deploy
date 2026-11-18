"""Turn the wizard's flat key/value output into the five env files."""

from __future__ import annotations

from pathlib import Path

from . import ui
from .envfile import drop, load, render, write_atomic
from .schema import FILE_MODES, PREVIOUS_FILE, TEMPLATES, EnvFile, available_files, is_secret_name, split_by_file


def write_env_files(repo: Path, values: dict[str, str], dry_run: bool = False) -> list[Path]:
    """Render each file over the operator's current copy (or the shipped
    template when there is none) and write it atomically with the right mode.
    Returns the paths written."""
    files = available_files(repo)
    if EnvFile.ENV not in files:
        raise FileNotFoundError(f"{repo}/env.example is missing; is this an Orizon checkout?")
    per_file = split_by_file(values, files)
    moved = {f: {k for k, old in PREVIOUS_FILE.items() if old is f and k not in per_file.get(f, {}) and any(k in per_file.get(o, {}) for o in files if o is not f)} for f in files}
    written: list[Path] = []
    for env_file in files:
        keys = per_file.get(env_file, {})
        target = repo / env_file.value
        stale = moved[env_file] & set(load(target)) if target.exists() else set()
        if not keys and not stale and env_file is not EnvFile.ENV:
            # Nothing of ours goes in this file. Seed it from the template if it
            # does not exist yet, so a part of the stack enabled by hand later
            # finds a documented file rather than none; never touch an existing one.
            if not target.exists():
                base = (repo / TEMPLATES[env_file]).read_text(encoding="utf-8")
                if not dry_run:
                    write_atomic(target, base, FILE_MODES[env_file])
                written.append(target)
            continue
        base_path = target if target.exists() else repo / TEMPLATES[env_file]
        base = base_path.read_text(encoding="utf-8")
        text = drop(render(base, keys), stale)
        if not dry_run:
            write_atomic(target, text, FILE_MODES[env_file])
        written.append(target)
    return written


def describe(values: dict[str, str], files: set[EnvFile]) -> None:
    """Print what is about to be written, secrets masked."""
    per_file = split_by_file(values, files)
    for env_file in EnvFile:
        keys = per_file.get(env_file)
        if not keys:
            continue
        ui.step(env_file.value)
        for k in sorted(keys):
            v = keys[k]
            ui.kv(k, ui.mask(v) if is_secret_name(k) else (v if v != "" else ui.mask("")), width=40)
