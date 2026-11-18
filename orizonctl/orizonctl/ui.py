"""Terminal output helpers. Everything user-facing goes through here so the
CLI reads the same in every command."""

from __future__ import annotations

import click

OK = click.style("  ok  ", fg="green", bold=True)
WARN = click.style(" warn ", fg="yellow", bold=True)
FAIL = click.style(" FAIL ", fg="red", bold=True)
INFO = click.style(" info ", fg="blue", bold=True)


def header(text: str) -> None:
    click.echo("")
    click.echo(click.style(f"== {text} ", fg="cyan", bold=True) + click.style("=" * max(0, 60 - len(text)), fg="cyan"))


def step(text: str) -> None:
    click.echo(click.style(f"--> {text}", bold=True))


def ok(text: str) -> None:
    click.echo(f"[{OK}] {text}")


def warn(text: str) -> None:
    click.echo(f"[{WARN}] {text}", err=True)


def fail(text: str) -> None:
    click.echo(f"[{FAIL}] {text}", err=True)


def info(text: str) -> None:
    click.echo(f"[{INFO}] {text}")


def note(text: str) -> None:
    """Indented explanatory text under a prompt or a check."""
    for line in text.splitlines():
        click.echo(click.style(f"        {line}", dim=True))


def kv(key: str, value: str, width: int = 26) -> None:
    click.echo(f"  {key:<{width}} {value}")


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return click.style("(not set)", dim=True)
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * 8 + value[-keep:]


def die(text: str, code: int = 1) -> None:
    fail(text)
    raise SystemExit(code)
