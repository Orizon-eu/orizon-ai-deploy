"""Carrying the operator's env files across a release.

A new release ships new `env.example*` templates. Some keys are new and need
a value; some keys the operator already has may have a different default now;
some keys are gone. This module computes that difference per file and writes
the migrated file: the NEW template as the base, with every value the operator
had set back in place, so the result reads like the new template (comments,
order, new keys in their documented spot) while nothing the operator set is
lost. A key the new template no longer knows is kept under the "set by
orizonctl" marker rather than dropped, unless another new template declares
it: then the operator's value moves to that file. The previous file is in the snapshot
`update` took.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .envfile import _ACTIVE, _COMMENTED, load, parse, render, write_atomic
from .schema import FILE_MODES, TEMPLATES, EnvFile, available_files, is_secret_name

NEW = "new"  # in the new template, not in the operator's file: needs a value
CHANGED_DEFAULT = "default-changed"  # operator has it; the template's default moved since the previous release
REMOVED = "removed"  # operator has it; the new template no longer declares it
EXTRA = "extra"  # operator has it; no template ever declared it (set by orizonctl under the marker, or by hand)
MOVED = "moved"


@dataclass
class KeyChange:
    file: EnvFile
    key: str
    kind: str
    current: str | None  # what the operator's file holds (None when absent)
    old_default: str | None  # the previous template's value
    new_default: str | None  # the new template's value
    help: str = ""  # the comment block above the key in the new template
    moved: EnvFile | None = None

    @property
    def secret(self) -> bool:
        return is_secret_name(self.key)


@dataclass
class FilePlan:
    file: EnvFile
    changes: list[KeyChange] = field(default_factory=list)
    current: dict[str, str] = field(default_factory=dict)

    def of_kind(self, kind: str) -> list[KeyChange]:
        return [c for c in self.changes if c.kind == kind]


def template_help(template: str, key: str) -> str:
    """The comment lines directly above `KEY=` (or `# KEY=`) in a template,
    stopping at a blank line or at another key. Section banners (lines of
    dashes or equals signs) are left out."""
    lines = template.splitlines()
    for i, line in enumerate(lines):
        m = _ACTIVE.match(line) if not line.lstrip().startswith("#") else _COMMENTED.match(line)
        if not m or m.group(1) != key:
            continue
        block: list[str] = []
        j = i - 1
        while j >= 0:
            prev = lines[j]
            if not prev.strip() or not prev.lstrip().startswith("#"):
                break
            if _COMMENTED.match(prev):
                break
            text = prev.lstrip("#").strip()
            if re.fullmatch(r"[-=]{3,}", text):
                break
            block.insert(0, text)
            j -= 1
        return "\n".join(block).strip()
    return ""


def plan_file(env_file: EnvFile, new_template: str, current_text: str, old_template: str | None) -> FilePlan:
    new_vals = parse(new_template)
    old_vals = parse(old_template) if old_template is not None else None
    current = parse(current_text)
    plan = FilePlan(env_file, current=current)
    for key, new_default in new_vals.items():
        if key not in current:
            plan.changes.append(KeyChange(env_file, key, NEW, None, old_vals.get(key) if old_vals else None, new_default, template_help(new_template, key)))
        elif old_vals is not None and key in old_vals and old_vals[key] != new_default:
            plan.changes.append(KeyChange(env_file, key, CHANGED_DEFAULT, current[key], old_vals[key], new_default, template_help(new_template, key)))
    for key, value in current.items():
        if key not in new_vals and not _commented_in(new_template, key):
            was_declared = old_vals is not None and (key in old_vals or _commented_in(old_template or "", key))
            plan.changes.append(KeyChange(env_file, key, REMOVED if was_declared else EXTRA, value, old_vals.get(key) if old_vals else None, None))
    return plan


def _commented_in(template: str, key: str) -> bool:
    return any((m := _COMMENTED.match(l)) and m.group(1) == key for l in template.splitlines() if l.lstrip().startswith("#"))


def plan_migration(app_dir: Path, old_templates: dict[EnvFile, str] | None) -> list[FilePlan]:
    """One plan per env file that exists on the host and has a template in
    the (new) app directory. `old_templates` is the previous release's text
    per file, read before the deployment files were replaced; without it,
    only new and removed keys can be told apart."""
    plans = []
    for f in available_files(app_dir):
        target = app_dir / f.value
        if not target.exists():
            continue
        new_template = (app_dir / TEMPLATES[f]).read_text(encoding="utf-8")
        old = (old_templates or {}).get(f)
        plans.append(plan_file(f, new_template, target.read_text(encoding="utf-8"), old))
    _link_moves(plans)
    return plans


def _link_moves(plans: list[FilePlan]) -> None:
    left = {(c.key, p.file): c for p in plans for c in p.changes if c.kind in (REMOVED, EXTRA)}
    for p in plans:
        for c in p.of_kind(NEW):
            origin = next((c2 for (key, f), c2 in left.items() if key == c.key and f is not p.file), None)
            if origin is None:
                continue
            c.current, c.moved = origin.current, origin.file
            origin.kind, origin.moved = MOVED, p.file


def template_keys(app_dir: Path, env_file: EnvFile) -> set[str]:
    """Every key the new template declares, active or commented out."""
    p = app_dir / TEMPLATES[env_file]
    if not p.exists():
        return set()
    keys = set(parse(p.read_text(encoding="utf-8")))
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _COMMENTED.match(line)
        if m:
            keys.add(m.group(1))
    return keys


def read_templates(app_dir: Path) -> dict[EnvFile, str]:
    out = {}
    for f, name in TEMPLATES.items():
        p = app_dir / name
        if p.exists():
            out[f] = p.read_text(encoding="utf-8")
    return out


def apply_migration(app_dir: Path, plan: FilePlan, decisions: dict[str, str], dry_run: bool = False) -> Path:
    """Write the migrated file: the new template with the operator's current
    values set back in, then `decisions` (key -> value) on top. Returns the
    path (written unless dry_run)."""
    target = app_dir / plan.file.value
    base = (app_dir / TEMPLATES[plan.file]).read_text(encoding="utf-8")
    values = dict(load(target))
    for c in plan.of_kind(MOVED):
        values.pop(c.key, None)
    values.update(decisions)
    text = render(base, values)
    if not dry_run:
        write_atomic(target, text, FILE_MODES[plan.file])
    return target


def summary(plans: list[FilePlan]) -> dict[str, int]:
    out = {NEW: 0, CHANGED_DEFAULT: 0, REMOVED: 0, EXTRA: 0, MOVED: 0}
    for p in plans:
        for c in p.changes:
            out[c.kind] += 1
    return out
