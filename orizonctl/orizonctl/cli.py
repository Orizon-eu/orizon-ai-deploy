"""orizonctl: the Orizon AI deployment CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from . import __version__, ui
from . import backup as bk
from . import envfile as _envfile
from . import hostinfo as h
from . import migrate as mig
from . import pins as _pins
from .assets import app_dir_is_materialized, is_bundle_available, materialize
from .config import ConfigError, example_names, example_text, load_config
from .compose import APP_TIMEOUT, MODEL_TIMEOUT, ComposeError, Stack, check_custom_cert, ensure_dirs, image_accessible
from .envfile import load, missing_keys
from .preflight import apply_host_fixes, has_failures, print_checks, run_checks
from .schema import FILE_MODES, PROBE_IMAGE, TEMPLATES, EnvFile, available_files, is_secret_name
from .state import STATE_DIR, State
from .wizard import Wizard, WizardError, current_values, load_answers
from .writer import describe, write_env_files

DEFAULT_HOME = "/opt/orizon"
HOME_CONFIG = "config.json"


# ------------------------------------------------------------------ context


class Ctx:
    def __init__(self, app_dir: Path, home: Path):
        self.app_dir = app_dir
        self.home = home
        self.stack = Stack(app_dir)
        self.sync_pins()

    def sync_pins(self) -> None:
        """Bring the per-service image override files in line with the state
        (they are derived from it; see pins.py)."""
        if not (self.app_dir / STATE_DIR).is_dir():
            return
        try:
            _pins.sync(self.app_dir, self.state)
        except OSError as e:
            ui.warn(f"could not write the image pin files under {self.app_dir / STATE_DIR}: {e}")

    def require_app(self) -> Path:
        if not app_dir_is_materialized(self.app_dir):
            raise click.ClickException(
                f"{self.app_dir} holds no deployment files. Run `orizonctl init` (writes the bundled compose files and templates there), or pass --app-dir to an existing checkout."
            )
        return self.app_dir

    @property
    def state(self) -> State | None:
        return State.load(self.app_dir)

    def require_state(self) -> State:
        self.require_app()
        st = self.state
        if st is None:
            raise click.ClickException("not configured yet: run `orizonctl configure` (or `orizonctl setup`) first")
        return st

    @property
    def values(self) -> dict[str, str]:
        return current_values(self.app_dir)

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    # $ORIZON_HOME/config.json: written by install.sh, read by `update`
    def home_config(self) -> dict:
        p = self.home / HOME_CONFIG
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            return {}

    def save_home_config(self, data: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / HOME_CONFIG).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resolve_app_dir(explicit: str | None, home: Path, home_is_default: bool) -> Path:
    """--app-dir / ORIZON_APP_DIR are authoritative (init may create them).
    Otherwise $ORIZON_HOME/app. Only when nobody named a home at all does a
    checkout at the current directory count, so a developer can run the CLI
    inside a monorepo clone without `init` ever writing into it by accident."""
    chosen = explicit or os.environ.get("ORIZON_APP_DIR")
    if chosen:
        return Path(chosen).resolve()
    if app_dir_is_materialized(home / "app") or not home_is_default:
        return (home / "app").resolve()
    if app_dir_is_materialized(Path.cwd()):
        return Path.cwd().resolve()
    return (home / "app").resolve()


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 110})
@click.option("--home", envvar="ORIZON_HOME", default=DEFAULT_HOME, show_default=True, help="Install root: app files, venv, backups.")
@click.option("--app-dir", envvar="ORIZON_APP_DIR", default=None, help="Directory holding docker-compose.yml and the .env files. Defaults to $ORIZON_HOME/app, or the current directory if it is a checkout.")
@click.version_option(__version__, prog_name="orizonctl")
@click.pass_context
def main(ctx: click.Context, home: str, app_dir: str | None) -> None:
    """Deploy and operate Orizon AI on this host.

    First time: `orizonctl setup`. Afterwards: `up`, `down`, `status`, `logs`,
    `doctor`, `backup`, `update`. Re-run `configure` to change anything.
    """
    home_is_default = home == DEFAULT_HOME and not os.environ.get("ORIZON_HOME")
    ctx.obj = Ctx(resolve_app_dir(app_dir, Path(home), home_is_default), Path(home))


# --------------------------------------------------------------------- init


def _materialize(ctx: Ctx, force: bool) -> None:
    if not is_bundle_available():
        raise click.ClickException("this orizonctl build carries no deployment files (assets missing). Reinstall it from the published package, or point --app-dir at a repository checkout.")
    ctx.app_dir.mkdir(parents=True, exist_ok=True)
    rep = materialize(ctx.app_dir, force=force)
    for rel in rep.written:
        ui.ok(f"wrote {rel}")
    if rep.unchanged:
        ui.info(f"{len(rep.unchanged)} files already current")
    for rel in rep.kept_local:
        ui.warn(f"kept your edited copy of {rel} (bundled version differs; `orizonctl init --force` replaces it and saves yours under .orizon/local-edits/)")
    for rel in rep.overwritten_local:
        ui.warn(f"replaced your edited {rel}; previous copy saved under {rep.backup_dir}")


@main.command()
@click.option("--force", is_flag=True, help="Replace files you edited (a copy is kept under .orizon/local-edits/).")
@pass_ctx
def init(ctx: Ctx, force: bool) -> None:
    """Write the bundled deployment files (compose, templates, scripts) into the app directory."""
    ui.header(f"Deployment files -> {ctx.app_dir}")
    _materialize(ctx, force)


# ---------------------------------------------------------------- preflight


@main.command()
@click.option("--strict", is_flag=True, help="Exit 1 on any failed check.")
@click.option("--gpu-smoke", is_flag=True, help="Also run nvidia-smi inside a throwaway container (pulls debian:bookworm-slim).")
@pass_ctx
def preflight(ctx: Ctx, strict: bool, gpu_smoke: bool) -> None:
    """Check the host: OS, GPU, Docker, registry access, ports, disk, kernel bits."""
    ui.header(f"Preflight on {h.os_release().get('PRETTY_NAME', h.arch())}")
    checks = run_checks(ctx.app_dir, ctx.state, ctx.values, gpu_smoke=gpu_smoke)
    print_checks(checks)
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    click.echo("")
    (ui.fail if fails else ui.ok)(f"{fails} failed, {warns} warnings")
    if strict and fails:
        raise SystemExit(1)


# ---------------------------------------------------------------- configure


def _configure(ctx: Ctx, interactive: bool, answers_path: str | None, dry_run: bool) -> tuple[dict[str, str], State]:
    ctx.require_app()
    answers = load_answers(Path(answers_path)) if answers_path else None
    if not interactive and answers is None and not ctx.values:
        raise click.ClickException("--non-interactive needs --config on a first run (nothing to reuse yet)")
    wiz = Wizard(ctx.app_dir, interactive=interactive, answers=answers, previous=ctx.state)
    values, state = wiz.run()
    previous = ctx.state
    if previous is not None:
        # not wizard questions: a service moved with `update <service>` stays where it is
        state.image_pins, state.mcp_image_pins = previous.image_pins, previous.mcp_image_pins
        state.extra_services = previous.extra_services  # nor one added with `service add`

    ui.header("Files")
    describe(values, available_files(ctx.app_dir))
    if interactive and not click.confirm("\nWrite these files?", default=True):
        raise click.Abort()
    written = write_env_files(ctx.app_dir, values, dry_run=dry_run)
    for p in written:
        (ui.info if dry_run else ui.ok)(("would write " if dry_run else "wrote ") + str(p))
    if not dry_run:
        p = state.save(ctx.app_dir)
        ui.ok(f"saved deployment state to {p}")
        err = ctx.stack.validate_config()
        if err:
            ui.warn("docker compose config reports a problem with the generated files:")
            ui.note(err)
        else:
            ui.ok("docker compose config parses the generated files")
    return values, state


CONFIG_HELP = "YAML (or JSON) config file; its `configure:` section answers the wizard's questions. Questions it answers are not asked. `orizonctl config example` prints one."


@main.command()
@click.option("--non-interactive", is_flag=True, help="No prompts: config file + existing values + defaults, or fail.")
@click.option("-c", "--config", "--answers", "config_path", type=click.Path(exists=True, dir_okay=False), help=CONFIG_HELP)
@click.option("--dry-run", is_flag=True, help="Show what would be written, write nothing.")
@pass_ctx
def configure(ctx: Ctx, non_interactive: bool, config_path: str | None, dry_run: bool) -> None:
    """Run the configuration wizard and write the .env* files."""
    try:
        _configure(ctx, interactive=not non_interactive, answers_path=config_path, dry_run=dry_run)
    except (WizardError, ConfigError) as e:
        raise click.ClickException(str(e))


# ---------------------------------------------------------------------- up


def _download_models_if_wanted(ctx: Ctx, state: State, values: dict[str, str], force_download: bool) -> None:
    if not (state.download_models or force_download):
        return
    from .models import catalog, download

    items = catalog(state, values, download=force_download)
    if not items:
        return
    ui.header("Model weights")
    ok = download(Path(values["MODELS_PATH"]), items, values.get("HUGGINGFACE_TOKEN"))
    if not ok:
        ui.warn("some downloads failed; the matching server will not come up until its weights are present")


def _run_setup_ssl(ctx: Ctx) -> bool:
    script = ctx.app_dir / "setup-ssl.sh"
    if not script.exists():
        ui.warn("setup-ssl.sh not in the app directory; skipping certificate issuance")
        return False
    ui.header("TLS certificate (setup-ssl.sh)")
    r = subprocess.run(["bash", str(script)], cwd=str(ctx.app_dir))
    if r.returncode != 0:
        ui.warn("setup-ssl.sh exited non-zero. nginx stays on the HTTP-only template; fix the cause and run `orizonctl ssl issue`.")
        return False
    return True


def _check_registry(state: State, values: dict[str, str]) -> None:
    ref = f"{values.get('ORIZON_IMAGE_PREFIX') or state.image_prefix}/{PROBE_IMAGE}:{values.get('ORIZON_VERSION') or state.version}"
    ok, msg = image_accessible(ref)
    if not ok:
        hint = f"docker login {state.registry_host}" if state.registry_host != "docker.io" else "docker login"
        raise click.ClickException(f"cannot reach {ref}: {msg}. Check ORIZON_IMAGE_PREFIX / ORIZON_VERSION and the registry login ({hint}), or re-run `orizonctl configure`.")
    ui.ok(f"registry: {ref} is pullable")


# Failures `up` cannot be talked past: without these nothing would start.
from .preflight import HARD_CHECK_NAMES as HARD_CHECKS  # noqa: E402


def _gate_preflight(checks, interactive: bool) -> None:
    """Decide whether `up` may proceed after a preflight with failures. Hard
    failures always stop. Others (memory, ports, GPU, fuse, registry) stop in a
    non-interactive run and ask in an interactive one: a 12 GB test VM is a
    legitimate place to run the CPU-only stack."""
    if not has_failures(checks):
        return
    hard = [c.name for c in checks if c.status == "fail" and c.name in HARD_CHECKS]
    if hard:
        raise click.ClickException(f"preflight failed on {', '.join(hard)}; these cannot be skipped")
    soft = [c.name for c in checks if c.status == "fail"]
    if not interactive:
        raise click.ClickException(f"preflight failed ({', '.join(soft)}); fix the items above or pass --skip-preflight")
    ui.warn(f"preflight failed on: {', '.join(soft)}")
    if not click.confirm("Continue anyway?", default=False):
        raise click.Abort()


def _up(ctx: Ctx, pull: bool, build: bool, skip_preflight: bool, download: bool) -> bool:
    state = ctx.require_state()
    values = ctx.values

    if not skip_preflight:
        ui.header("Preflight")
        checks = run_checks(ctx.app_dir, state, values)
        print_checks(checks)
        _gate_preflight(checks, interactive=sys.stdin.isatty() and sys.stdout.isatty())

    ui.header("Host preparation")
    ensure_dirs(ctx.app_dir, state, values)
    apply_host_fixes(ctx.app_dir, state)
    if state.access_mode == "custom-cert":
        problem = check_custom_cert(ctx.app_dir, values)
        if problem:
            raise click.ClickException(f"custom certificate: {problem}. Place the files and re-run.")
    err = ctx.stack.validate_config()
    if err:
        raise click.ClickException(f"docker compose config rejects the current files:\n{err}")
    if pull and not build:
        _check_registry(state, values)

    _download_models_if_wanted(ctx, state, values, download)

    ui.header("Stack")
    ok = ctx.stack.bring_up(state, values, pull=pull, build=build, defer_model_wait=True)

    if ok and state.access_mode == "letsencrypt":
        _run_setup_ssl(ctx)

    ok &= ctx.stack.wait_models(ctx.stack.pending_models)
    return bool(ok)


def _summary(ctx: Ctx, ok: bool) -> None:
    state = ctx.require_state()
    ui.header("Done" if ok else "Finished with problems")
    ui.kv("App URL", state.public_url)
    ui.kv("Status", "orizonctl status   |   orizonctl doctor")
    ui.kv("Logs", "orizonctl logs backend   (or any service)")
    ui.kv("Admin panel", "sign in, then Settings > System for credentials and runtime settings")
    if state.mcp:
        ui.kv("Connectors", "Settings > System > Connectors (tenants, OAuth apps, sandbox policy)")
    if state.access_mode == "letsencrypt" and ctx.values.get("CERTBOT_STAGING", "true") == "true":
        ui.kv("Certificate", "STAGING (browser warning expected). Set staging off in `orizonctl configure`, then `orizonctl ssl issue`.")
    ui.kv("Log cleanup", "orizonctl systemd install   (daily timer)")


@main.command()
@click.option("--no-pull", is_flag=True, help="Do not pull images first.")
@click.option("--build", is_flag=True, help="Build images from a source checkout instead of pulling (needs submodules).")
@click.option("--skip-preflight", is_flag=True)
@click.option("--download-models", is_flag=True, help="Download missing model weights even if the wizard said no.")
@pass_ctx
def up(ctx: Ctx, no_pull: bool, build: bool, skip_preflight: bool, download_models: bool) -> None:
    """Start (or update) the configured stack and wait until it is healthy."""
    try:
        ok = _up(ctx, pull=not no_pull, build=build, skip_preflight=skip_preflight, download=download_models)
    except ComposeError as e:
        raise click.ClickException(str(e))
    _summary(ctx, ok)
    if not ok:
        raise SystemExit(1)


@main.command()
@click.option("--non-interactive", is_flag=True, help="Unattended: needs --config.")
@click.option("-c", "--config", "--answers", "config_path", type=click.Path(exists=True, dir_okay=False), help=CONFIG_HELP)
@click.option("--no-pull", is_flag=True)
@click.option("--build", is_flag=True, help="Build from a source checkout instead of pulling images.")
@pass_ctx
def setup(ctx: Ctx, non_interactive: bool, config_path: str | None, no_pull: bool, build: bool) -> None:
    """First-time install: deployment files, preflight, configure, download models, start, verify."""
    ui.header(f"Orizon AI setup ({ctx.app_dir})")
    if not app_dir_is_materialized(ctx.app_dir):
        _materialize(ctx, force=False)
    # Only what decides whether the wizard can run. The full preflight comes
    # after configuration, inside `up`, so it judges the answers just given.
    checks = run_checks(ctx.app_dir, ctx.state, ctx.values, quick=True)
    print_checks(checks)
    if has_failures(checks):
        raise click.ClickException("fix the failed checks above, then re-run `orizonctl setup`")
    try:
        _configure(ctx, interactive=not non_interactive, answers_path=config_path, dry_run=False)
        ok = _up(ctx, pull=not no_pull, build=build, skip_preflight=False, download=False)
    except (WizardError, ConfigError, ComposeError) as e:
        raise click.ClickException(str(e))
    _summary(ctx, ok)
    if not ok:
        raise SystemExit(1)


# ------------------------------------------------------------- lifecycle


@main.command()
@click.option("--volumes", is_flag=True, help="ALSO DELETE the database, model cache and certificate volumes.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_ctx
def down(ctx: Ctx, volumes: bool, yes: bool) -> None:
    """Stop the stack (main + MCP). Data volumes are kept unless --volumes."""
    ctx.require_app()
    if volumes and not yes:
        click.confirm(click.style("This deletes the database, the vLLM cache and every TLS certificate. Continue?", fg="red", bold=True), abort=True)
        typed = click.prompt("Type DELETE ALL DATA to confirm")
        if typed != "DELETE ALL DATA":
            raise click.Abort()
    if ctx.stack.has_file(mcp=True):
        ctx.stack.down(mcp=True, volumes=volumes)
    ctx.stack.down(volumes=volumes)
    ui.ok("stack is down" + (" and volumes removed" if volumes else ""))


@main.command()
@click.argument("services", nargs=-1)
@click.option("--mcp", is_flag=True, help="Target the MCP compose file.")
@pass_ctx
def restart(ctx: Ctx, services: tuple[str, ...], mcp: bool) -> None:
    """Restart services (default: the whole configured stack)."""
    st = ctx.require_state()
    targets = list(services) or ([] if mcp else st.all_services())
    try:
        ctx.stack.restart(targets, mcp=mcp)
    except ComposeError as e:
        raise click.ClickException(str(e))
    ui.ok("restarted " + (", ".join(targets) or "MCP stack"))


@main.command()
@pass_ctx
def status(ctx: Ctx) -> None:
    """Container status for both stacks."""
    ctx.require_app()
    ctx.stack.ps()
    if ctx.stack.has_file(mcp=True) and ctx.state and ctx.state.mcp:
        click.echo("")
        ctx.stack.ps(mcp=True)


@main.command()
@click.argument("services", nargs=-1)
@click.option("-f", "--follow", is_flag=True)
@click.option("-n", "--tail", default=200, show_default=True)
@click.option("--mcp", is_flag=True, help="Read from the MCP compose file.")
@pass_ctx
def logs(ctx: Ctx, services: tuple[str, ...], follow: bool, tail: int, mcp: bool) -> None:
    """Show service logs."""
    ctx.require_app()
    ctx.stack.logs(list(services), follow, tail, mcp=mcp)


@main.command()
@pass_ctx
def pull(ctx: Ctx) -> None:
    """Pull the images for the configured services at ORIZON_VERSION."""
    st = ctx.require_state()
    _check_registry(st, ctx.values)
    try:
        ctx.stack.pull([s for s in st.all_services() if s in ctx.stack.config_services()])
        if st.mcp and ctx.stack.has_file(mcp=True):
            services, _ = ctx.stack.connector_services(ctx.values.get("ORIZON_IMAGE_PREFIX") or st.image_prefix)
            ctx.stack.pull(services, mcp=True)
    except ComposeError as e:
        raise click.ClickException(str(e))
    ui.ok("images pulled")


# ---------------------------------------------------------------- doctor


@main.command()
@pass_ctx
def doctor(ctx: Ctx) -> None:
    """Health report: containers, endpoints, GPU, disk, certificate."""
    from .doctor import report

    st = ctx.require_state()
    ui.header("Orizon AI health")
    checks = report(ctx.stack, st, ctx.values)
    print_checks(checks)
    fails = sum(c.status == "fail" for c in checks)
    click.echo("")
    (ui.fail if fails else ui.ok)(f"{fails} problems")
    if fails:
        raise SystemExit(1)


# ---------------------------------------------------------------- backup


def _snapshot(ctx: Ctx, kind: str, to_version: str = "", include_db: bool = True, include_data: bool = True) -> bk.Snapshot:
    ctx.require_app()
    return bk.create_snapshot(
        ctx.home, ctx.app_dir, ctx.stack, ctx.values, ctx.state,
        kind=kind, to_version=to_version, include_db=include_db, include_data=include_data,
        cli_source=ctx.home_config().get("cli_source", ""),
    )


@main.group(invoke_without_command=True)
@click.option("--no-data", is_flag=True, help="Skip the client data archive (uploads, indexes, bot sessions, certs).")
@click.option("--no-db", is_flag=True, help="Skip the database dump.")
@click.option("--no-env", is_flag=True, hidden=True)  # old flag: the env files are always small, always included
@click.pass_context
def backup(click_ctx: click.Context, no_data: bool, no_db: bool, no_env: bool) -> None:
    """Snapshot the deployment into $ORIZON_HOME/backups/<ts>_manual_<version>/.

    Database dump (pg_dump -Fc), env files + state, the deployment files of
    this release, the client data directories and, in Let's Encrypt mode, the
    certificates. `orizonctl rollback <name>` restores one. Sub-commands:
    `list`, `prune`.
    """
    if click_ctx.invoked_subcommand is not None:
        return
    ctx: Ctx = click_ctx.obj
    ui.header("Backup")
    try:
        snap = _snapshot(ctx, "manual", include_db=not no_db, include_data=not no_data)
    except ComposeError as e:
        raise click.ClickException(str(e))
    ui.note(f"restore everything: orizonctl rollback {snap.name}\nthe database alone: orizonctl rollback {snap.name} --db-only")


@backup.command("list")
@pass_ctx
def backup_list(ctx: Ctx) -> None:
    """The snapshots under $ORIZON_HOME/backups, oldest first."""
    snaps = bk.list_snapshots(ctx.home)
    if not snaps:
        ui.info(f"no snapshots under {ctx.backups}")
        return
    for s in snaps:
        m = s.manifest
        parts = [k for k in ("db", "data", "files", "letsencrypt") if k in m.contents]
        arrow = f" -> {m.to_version}" if m.to_version else ""
        click.echo(f"  {s.name:<40} {m.kind:<7} {m.from_version}{arrow:<12} {m.status:<12} {', '.join(parts)}")


@backup.command("prune")
@click.option("--keep", default=5, show_default=True, help="How many of the newest snapshots to keep.")
@click.option("--yes", is_flag=True)
@pass_ctx
def backup_prune(ctx: Ctx, keep: int, yes: bool) -> None:
    """Delete all but the newest N snapshots."""
    snaps = bk.list_snapshots(ctx.home)
    doomed = snaps[:-keep] if keep > 0 else snaps
    if not doomed:
        ui.info("nothing to prune")
        return
    for s in doomed:
        click.echo(f"  {s.name}")
    if not yes:
        click.confirm(f"Delete these {len(doomed)} snapshots?", abort=True)
    bk.prune(ctx.home, keep)
    ui.ok(f"deleted {len(doomed)} snapshots; {min(keep, len(snaps))} kept")


# ---------------------------------------------------------------- update


def resolve_cli_spec(template: str, version: str) -> str:
    """Turn the pip-spec template recorded at install time into the spec for
    one version. `{version}` is the tag (v1.3.0), `{pyversion}` the PEP 440
    form (1.3.0)."""
    return template.format(version=version, pyversion=version.lstrip("v"))


def _on_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _self_bin() -> str:
    cand = Path(sys.prefix) / "bin" / "orizonctl"
    return str(cand) if cand.exists() else "orizonctl"


_RELEASE_TAG = __import__("re").compile(r"^v\d+\.\d+\.\d+$")


def latest_release(tags: list[str]) -> str | None:
    """The newest plain release tag (v1.3.0, not v1.3.0_dev); the newest of
    anything when no plain release exists."""
    from .registry import newest

    plain = [t for t in tags if _RELEASE_TAG.match(t)]
    return newest(plain) or newest(tags)


def _resolve_target(ctx: Ctx, st: State, version: str | None, latest: bool, interactive: bool) -> str | None:
    """The release to move to: --version, or the registry's newest for
    --latest, or None (re-pull the current one)."""
    from . import registry as _registry

    if version and latest:
        raise click.ClickException("--version and --latest are exclusive")
    if version:
        if not version.startswith("v"):
            raise click.ClickException("releases look like v1.3.0")
        return version
    if not latest:
        return None
    prefix = ctx.values.get("ORIZON_IMAGE_PREFIX") or st.image_prefix
    tags = _registry.list_tags(prefix, PROBE_IMAGE)
    if not tags:
        raise click.ClickException(f"could not list the tags of {prefix}/{PROBE_IMAGE} (no permission to list, or unreachable); pass --version instead")
    target = latest_release(tags)
    shown = tags[-8:]
    ui.info(f"registry tags: {', '.join(shown)}" + (" ..." if len(tags) > len(shown) else ""))
    if target == st.version:
        ui.ok(f"{st.version} is already the newest release")
        return None
    if _registry.version_key(target) < _registry.version_key(st.version):
        ui.warn(f"the newest release in the registry ({target}) is older than what runs here ({st.version})")
        if not interactive or not click.confirm(f"Move to {target} anyway?", default=False):
            return None
    return target


def _pull_target(ctx: Ctx, st: State, target: str) -> None:
    """Pull every configured service at the target tag before anything on disk
    changes, so a wrong tag or a dead registry is found while the old stack
    still runs untouched."""
    values = ctx.values
    prefix = values.get("ORIZON_IMAGE_PREFIX") or st.image_prefix
    ref = f"{prefix}/{PROBE_IMAGE}:{target}"
    ok, msg = image_accessible(ref)
    if not ok:
        hint = f"docker login {st.registry_host}" if st.registry_host != "docker.io" else "docker login"
        raise click.ClickException(f"cannot reach {ref}: {msg}. Is {target} pushed? Check the registry login ({hint}).")
    ui.ok(f"registry: {ref} is pullable")
    services = [s for s in st.all_services() if s in ctx.stack.config_services()]
    ui.step(f"Pulling images at {target}")
    ctx.stack.pull(services, env={"ORIZON_VERSION": target})
    if st.mcp and ctx.stack.has_file(mcp=True):
        connectors, _ = ctx.stack.connector_services(prefix, env={"ORIZON_VERSION": target})
        ctx.stack.pull(connectors, mcp=True, env={"ORIZON_VERSION": target})


def _parse_sets(sets: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in sets:
        if "=" not in item:
            raise click.ClickException(f"--set expects KEY=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v
    return out


def _show_change(c: mig.KeyChange) -> None:
    shown = (lambda v: ui.mask(v) if c.secret else (v if v not in (None, "") else "(empty)"))
    if c.kind == mig.NEW and c.moved is not None:
        ui.kv(c.key, f"moved here from {c.moved.value}; yours: {shown(c.current)}   template default: {shown(c.new_default)}", width=44)
    elif c.kind == mig.NEW:
        ui.kv(c.key, f"new in this release; template default: {shown(c.new_default)}", width=44)
    elif c.kind == mig.MOVED:
        ui.kv(c.key, f"moved to {c.moved.value}; your value goes with it", width=44)
    elif c.kind == mig.CHANGED_DEFAULT:
        ui.kv(c.key, f"yours: {shown(c.current)}   new default: {shown(c.new_default)}   (was {shown(c.old_default)})", width=44)
    else:
        ui.kv(c.key, f"no longer in the template; kept as is ({shown(c.current)})", width=44)


def _migrate_env(ctx: Ctx, old_templates: dict[EnvFile, str], interactive: bool, sets: dict[str, str], keep_existing: bool, dry_run: bool = False) -> list[Path]:
    """Carry the env files across the new templates. New keys need a value
    (asked, or --set, or the template default); keys whose default moved are
    kept unless the operator says otherwise; any other key can be changed on
    request. Every migrated file is re-based on its new template so new keys
    sit in their documented place; the previous copy is in the snapshot."""
    plans = mig.plan_migration(ctx.app_dir, old_templates)
    counts = mig.summary(plans)
    written: list[Path] = []
    if not any(c.kind != mig.EXTRA for p in plans for c in p.changes):
        ui.ok("env files: every template key is present and no default moved; nothing to migrate")
        return written
    ui.info(f"{counts[mig.NEW] - counts[mig.MOVED]} new keys, {counts[mig.MOVED]} moved to another file, {counts[mig.CHANGED_DEFAULT]} keys whose default changed, {counts[mig.REMOVED]} keys the new templates no longer declare")
    for plan in plans:
        decisions: dict[str, str] = {}
        if all(c.kind == mig.EXTRA for c in plan.changes):
            continue
        ui.step(plan.file.value)
        extra = [c.key for c in plan.of_kind(mig.EXTRA)]
        if extra:
            ui.info(f"{len(extra)} keys outside the template are kept: {', '.join(extra[:8])}{' ...' if len(extra) > 8 else ''}")
        for c in plan.of_kind(mig.NEW):
            _show_change(c)
            if c.key in sets:
                decisions[c.key] = sets.pop(c.key)
                ui.ok(f"{c.key}: from --set")
            elif c.moved is not None:
                decisions[c.key] = c.current or ""
            elif interactive:
                if c.help:
                    ui.note(c.help)
                decisions[c.key] = click.prompt(f"    {c.key}", default=c.new_default if c.new_default is not None else "", show_default=not c.secret, hide_input=c.secret, type=str)
            else:
                decisions[c.key] = c.new_default or ""
        for c in plan.of_kind(mig.CHANGED_DEFAULT):
            _show_change(c)
            if c.key in sets:
                decisions[c.key] = sets.pop(c.key)
                ui.ok(f"{c.key}: from --set")
            elif interactive and not keep_existing:
                if c.help:
                    ui.note(c.help)
                choice = click.prompt(f"    {c.key}: [k]eep yours, take the [n]ew default, or [e]nter a value", default="k", type=click.Choice(["k", "n", "e"], case_sensitive=False), show_choices=False)
                if choice == "n":
                    decisions[c.key] = c.new_default or ""
                elif choice == "e":
                    decisions[c.key] = click.prompt(f"    {c.key}", default=c.current, show_default=not c.secret, hide_input=c.secret, type=str)
        for c in plan.of_kind(mig.REMOVED) + plan.of_kind(mig.MOVED):
            _show_change(c)
        # anything else the operator wants to change in this file
        for key in [k for k in list(sets) if k in plan.current or k in mig.template_keys(ctx.app_dir, plan.file)]:
            decisions[key] = sets.pop(key)
            ui.ok(f"{key}: from --set")
        if interactive:
            while True:
                raw = click.prompt(f"    Change another key in {plan.file.value}? (KEY or KEY=VALUE, Enter to continue)", default="", show_default=False, type=str).strip()
                if not raw:
                    break
                key, _, val = raw.partition("=")
                key = key.strip()
                if not key:
                    continue
                if "=" not in raw:
                    cur = plan.current.get(key, "")
                    ui.kv(key, ui.mask(cur) if is_secret_name(key) else (cur or "(empty)"), width=44)
                    val = click.prompt(f"    {key}", default=cur, show_default=not is_secret_name(key), hide_input=is_secret_name(key), type=str)
                decisions[key] = val
        target = mig.apply_migration(ctx.app_dir, plan, decisions, dry_run=dry_run)
        written.append(target)
        (ui.info if dry_run else ui.ok)(("would migrate " if dry_run else "migrated ") + str(target) + (f" ({len(decisions)} values set)" if decisions else ""))
    for key in sets:
        ui.warn(f"--set {key}: no env file declares this key; not written")
    return written


def _failed_update_hint(snap: bk.Snapshot | None, interactive: bool) -> None:
    if snap is None:
        return
    ui.warn(f"the previous state is in snapshot {snap.name}")
    if interactive and click.confirm("Roll back to it now (stack down, files + database restored, previous release started)?", default=False):
        click.get_current_context().invoke(rollback, snapshot=snap.name, yes=True, no_db=False, no_data=True, db_only=False, no_pull=False, no_cli=False)
    else:
        ui.note(f"orizonctl rollback {snap.name}")


def _fetch_image(ctx: Ctx, st: State, ref: str) -> None:
    """Get `ref` onto this host before anything changes: pulled when a registry
    serves it, else accepted when a copy is already here (a locally loaded build)."""
    reachable, msg = image_accessible(ref)
    if reachable:
        ctx.stack.pull_image(ref)
        ui.ok(f"pulled {ref}")
    elif ctx.stack.docker("image", "inspect", ref, check=False).returncode == 0:
        ui.warn(f"{ref} is not in a registry this host can reach ({msg}); using the copy already on this host")
    else:
        hint = f"docker login {st.registry_host}" if st.registry_host != "docker.io" else "docker login"
        raise click.ClickException(f"cannot reach {ref}: {msg}. Is it pushed? Check the registry login ({hint}). Nothing changed.")


def _update_services(ctx: Ctx, st: State, services: tuple[str, ...], version: str | None, image: str | None, unpin: bool, no_backup: bool, no_data: bool, interactive: bool, yes: bool) -> None:
    """Move the named services to another image and recreate only them. The
    release (ORIZON_VERSION, orizonctl, deployment files, env files) stays."""
    if sum(map(bool, (version, image, unpin))) > 1:
        raise click.ClickException("--version, --image and --unpin are exclusive")
    services = tuple(dict.fromkeys(services))
    if image and len(services) != 1:
        raise click.ClickException("--image names one image, so it takes exactly one service")
    if image and not _pins.valid_ref(image):
        raise click.ClickException(f"not an image reference: {image!r}")
    if version and not _pins.valid_ref(version):
        raise click.ClickException(f"not an image tag: {version!r}")

    has_mcp = st.mcp and ctx.stack.has_file(mcp=True)
    current = {False: ctx.stack.image_refs(), True: ctx.stack.image_refs(mcp=True) if has_mcp else {}}
    unknown = [s for s in services if s not in current[False] and s not in current[True]]
    if unknown:
        raise click.ClickException(f"not a service of the configured stack: {', '.join(unknown)}. Services: {', '.join(sorted({**current[False], **current[True]}))}")

    changes: list[tuple[str, bool, str, str, bool]] = []  # service, mcp, old ref, new ref, pinned
    for mcp in (False, True):
        group = [s for s in services if s in current[mcp]]
        if not group:
            continue
        base = ctx.stack.image_refs(mcp=mcp, pins=False)
        tagged = ctx.stack.image_refs(mcp=mcp, pins=False, env={"ORIZON_VERSION": version}) if version else {}
        for s in group:
            added = st.extra_services.get(s)
            if added is not None:
                # no release image underneath: the choice replaces the one `service add` recorded
                if unpin:
                    raise click.ClickException(f"{s} was added with `service add`, so there is no release image to return to; `orizonctl service remove {s}` takes it out")
                new = image or (_pins.retag(added["image"], version) if version else current[mcp][s])
                changes.append((s, mcp, current[mcp][s], new, False))
                continue
            if image:
                new = image
            elif version:
                new = tagged.get(s, "")
                if not new.endswith(":" + version):
                    raise click.ClickException(f"{s} runs {base.get(s) or '?'}, which does not follow ORIZON_VERSION; name its image with --image")
            elif unpin:
                new = base.get(s, "")
            else:
                new = current[mcp][s]
            if not new:
                raise click.ClickException(f"{s} has no image in the compose file")
            changes.append((s, mcp, current[mcp][s], new, new != base.get(s)))

    ui.header(f"Update {', '.join(services)} (the rest of the stack stays on {st.version})")
    for s, _mcp, old, new, pinned in changes:
        ui.kv(s, (f"{old} -> {new}" if new != old else f"{new} (re-pull + restart)") + ("   [added service]" if s in st.extra_services else "   [pinned]" if pinned else "   [follows ORIZON_VERSION]"), width=24)
    if not yes and interactive and not click.confirm("Continue?", default=True):
        raise click.Abort()

    snap: bk.Snapshot | None = None
    if not no_backup:
        ui.header("Snapshot")
        try:
            snap = _snapshot(ctx, "update", to_version=", ".join(f"{s}:{_pins.tag_of(new)}" for s, _m, _o, new, _p in changes), include_data=not no_data)
        except ComposeError as e:
            raise click.ClickException(f"snapshot failed, nothing changed: {e}")
    else:
        ui.warn("--no-backup: no snapshot, so no rollback afterwards")

    written = False
    try:
        ui.header("Images")
        for ref in dict.fromkeys(new for _s, _m, _o, new, _p in changes):
            _fetch_image(ctx, st, ref)

        for s, mcp, _old, new, pinned in changes:
            table = st.mcp_image_pins if mcp else st.image_pins
            if s in st.extra_services:
                st.extra_services[s]["image"] = new
            elif pinned:
                table[s] = new
            else:
                table.pop(s, None)
        st.save(ctx.app_dir)
        ctx.sync_pins()
        written = True

        ui.header("Restart")
        ok = True
        for mcp in (False, True):
            group = [s for s, m, _o, _n, _p in changes if m == mcp]
            if not group:
                continue
            ui.step(f"Recreating {', '.join(group)}")
            ctx.stack.recreate(group, mcp=mcp)
            timeout = MODEL_TIMEOUT if any(s in st.model_services() for s in group) else APP_TIMEOUT
            ok &= ctx.stack.wait(group, timeout, mcp=mcp, label="update")
        if ok and any(s in ("backend", "frontend", "nginx") and not m for s, m, _o, _n, _p in changes):
            ok &= ctx.stack._wait_http(ctx.values, st)
    except click.ClickException as e:  # ComposeError is one
        ui.fail(e.format_message())
        ok = False

    if snap:
        snap.manifest.status = "applied" if ok else "update-failed"
        snap.save()
    if not ok:
        if written:
            ui.note(f"the image choice is recorded; to go back to the release's image: orizonctl update {' '.join(s for s in services if s not in st.extra_services) or '<service>'} --unpin")
        _failed_update_hint(snap, interactive)
        raise SystemExit(1)
    ui.header("Done")
    pinned_now = {**st.image_pins, **st.mcp_image_pins}
    if pinned_now:
        ui.kv("Pinned", ", ".join(f"{s}={_pins.tag_of(r)}" for s, r in sorted(pinned_now.items())) + "   (`orizonctl update <service> --unpin` returns one to ORIZON_VERSION)")
    if snap:
        ui.kv("Rollback", f"orizonctl rollback {snap.name}")


@main.command()
@click.argument("services", nargs=-1)
@click.option("--version", "version", help="Release to move to, e.g. v1.3.0: upgrades orizonctl, its bundled files and ORIZON_VERSION together. With SERVICES: the image tag for those services only.")
@click.option("--image", help="With one SERVICE: the full image reference to run it on (registry/path/name:tag).")
@click.option("--unpin", is_flag=True, help="With SERVICES: return them to the release's image (ORIZON_VERSION).")
@click.option("--keep-pins", is_flag=True, help="On a release update, keep services pinned with `update SERVICE` on their image (default: every service moves to the release).")
@click.option("--latest", is_flag=True, help="Move to the newest release tag the registry holds.")
@click.option("--cli-source", help="pip spec template for the CLI (default: what install.sh recorded in $ORIZON_HOME/config.json).")
@click.option("--no-backup", is_flag=True, help="Skip the snapshot (no rollback possible afterwards).")
@click.option("--no-data", is_flag=True, help="Snapshot without the client data archive (faster on large deployments).")
@click.option("--no-cli-upgrade", is_flag=True, help="Only bump ORIZON_VERSION and restart; keep this orizonctl.")
@click.option("--force-assets", is_flag=True, help="Replace deployment files you edited by hand (copies kept under .orizon/local-edits/).")
@click.option("--non-interactive", is_flag=True, help="No questions: new env keys take --set or the template default, existing values are kept.")
@click.option("--set", "sets", multiple=True, metavar="KEY=VALUE", help="Value for an env key (new or existing); repeatable.")
@click.option("--keep-existing", is_flag=True, help="Do not ask about keys whose template default moved; keep every current value.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before changing anything.")
@click.option("--snapshot", "snapshot_name", hidden=True, help="(internal) the snapshot the first stage already took.")
@pass_ctx
def update(ctx: Ctx, services: tuple[str, ...], version: str | None, image: str | None, unpin: bool, keep_pins: bool, latest: bool, cli_source: str | None, no_backup: bool, no_data: bool, no_cli_upgrade: bool, force_assets: bool, non_interactive: bool, sets: tuple[str, ...], keep_existing: bool, yes: bool, snapshot_name: str | None) -> None:
    """Upgrade to a release, with a snapshot to roll back to.

    \b
    In order: resolve the target (--version, --latest, or the current one);
    snapshot (database, env files, deployment files, client data); pull the
    new images while the old stack still runs; upgrade orizonctl to the
    matching release; write the new deployment files; carry the env files
    across the new templates (new keys are asked for); bump ORIZON_VERSION;
    phased restart; verify. Database migrations run when the new frontend
    starts. If anything fails: `orizonctl rollback`.

    \b
    With SERVICES, only those move and nothing else is touched (no orizonctl
    upgrade, no file or env changes, ORIZON_VERSION stays):
      orizonctl update backend --version v1.3.1      another tag of its image
      orizonctl update backend --image reg/x/y:tag   any image
      orizonctl update backend                       re-pull its tag, restart
      orizonctl update backend --unpin               back to ORIZON_VERSION
    The choice is kept across `up` and restarts until --unpin or the next
    release update. Service names: `orizonctl info`.
    """
    st = ctx.require_state()
    interactive = not non_interactive and _on_terminal()
    set_values = _parse_sets(sets)

    if services:
        if latest or set_values:
            raise click.ClickException("--latest and --set belong to a release update; with service names use --version, --image or --unpin")
        try:
            _update_services(ctx, st, services, version, image, unpin, no_backup, no_data, interactive, yes)
        except ComposeError as e:
            raise click.ClickException(e.format_message())
        return
    if image or unpin:
        raise click.ClickException("--image and --unpin need a service name, e.g. `orizonctl update backend --unpin`")

    target = _resolve_target(ctx, st, version, latest, interactive)
    ui.header(f"Update {st.version} -> {target or st.version + ' (same release: re-pull + restart)'}")
    if not yes and interactive and not click.confirm("Continue?", default=True):
        raise click.Abort()

    snap: bk.Snapshot | None = None
    if snapshot_name:
        snap = bk.find_snapshot(ctx.home, snapshot_name)
    elif not no_backup:
        ui.header("Snapshot")
        try:
            snap = _snapshot(ctx, "update", to_version=target or st.version, include_data=not no_data)
        except ComposeError as e:
            raise click.ClickException(f"snapshot failed, nothing changed: {e}")
    else:
        ui.warn("--no-backup: no snapshot, so no rollback afterwards")

    try:
        if target:
            ui.header("Images")
            _pull_target(ctx, st, target)

        if target and not no_cli_upgrade:
            template = cli_source or ctx.home_config().get("cli_source")
            if not template:
                raise click.ClickException("no CLI source recorded (install.sh writes it to $ORIZON_HOME/config.json). Pass --cli-source, or --no-cli-upgrade to only move the images.")
            spec = resolve_cli_spec(template, target)
            ui.header(f"orizonctl {target}")
            ui.step(f"pip install --upgrade {spec}")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", spec], capture_output=True, text=True)
            if r.returncode != 0:
                raise click.ClickException(f"pip failed:\n{r.stderr.strip()[-1500:]}")
            if cli_source:
                ctx.save_home_config({**ctx.home_config(), "cli_source": cli_source})
            # from here on, the NEW code must do the work: hand over to the freshly installed binary
            newbin = _self_bin()
            ui.ok(f"installed; continuing with {newbin}")
            rest = [newbin, "--home", str(ctx.home), "--app-dir", str(ctx.app_dir), "update", "--version", target, "--no-backup", "--no-cli-upgrade", "--yes"]
            if snap:
                rest += ["--snapshot", snap.name]
            if force_assets:
                rest.append("--force-assets")
            if non_interactive:
                rest.append("--non-interactive")
            if keep_existing:
                rest.append("--keep-existing")
            if keep_pins:
                rest.append("--keep-pins")
            for k, v in set_values.items():
                rest += ["--set", f"{k}={v}"]
            code = subprocess.call(rest)
            if code != 0:
                _failed_update_hint(snap, interactive)
            raise SystemExit(code)

        ui.header("Deployment files")
        old_templates = mig.read_templates(ctx.app_dir)
        if is_bundle_available():
            _materialize(ctx, force=force_assets)
        else:
            ui.info("no bundled files in this build (source checkout); leaving the app directory as is")
        for name in _now_in_release(ctx, st):
            st.extra_services.pop(name)
            ui.ok(f"{name}: defined by this release's compose file now; the copy added with `service add` is dropped")

        ui.header("Env files")
        _migrate_env(ctx, old_templates, interactive, set_values, keep_existing)

        if target:
            write_env_files(ctx.app_dir, {"ORIZON_VERSION": target})
            st.version = target
            ui.ok(f"ORIZON_VERSION={target}")
            pinned = {**st.image_pins, **st.mcp_image_pins}
            if pinned and not keep_pins:
                ui.warn(f"image pins dropped, these move to {target} with the rest: {', '.join(sorted(pinned))}  (--keep-pins keeps them)")
                st.image_pins, st.mcp_image_pins = {}, {}
            elif pinned:
                ui.info(f"still pinned to their own image: {', '.join(sorted(pinned))}")
        st.save(ctx.app_dir)
        ctx.sync_pins()

        ok = _up(ctx, pull=True, build=False, skip_preflight=False, download=False)
    except (ComposeError, click.ClickException) as e:
        ui.fail(str(e))
        _failed_update_hint(snap, interactive)
        raise SystemExit(1)

    if snap:
        snap.manifest.status = "applied" if ok else "update-failed"
        snap.save()
    _summary(ctx, ok)
    if ok and snap:
        ui.kv("Rollback", f"orizonctl rollback {snap.name}")
    if not ok:
        _failed_update_hint(snap, interactive)
        raise SystemExit(1)


# ----------------------------------------------------------- self-update


@main.command("self-update")
@click.option("--ref", help="Tag or branch of the CLI source to install (default: the release this host runs, ORIZON_VERSION). `main` takes the newest published copy.")
@click.option("--cli-source", help="pip spec template for the CLI (default: what install.sh recorded in $ORIZON_HOME/config.json).")
@click.option("--no-files", is_flag=True, help="Only reinstall orizonctl; do not write its bundled deployment files into the app directory.")
@pass_ctx
def self_update(ctx: Ctx, ref: str | None, cli_source: str | None, no_files: bool) -> None:
    """Reinstall orizonctl itself, without moving the release.

    \b
    For a fix to the CLI or its bundled files that was published again under
    the same release tag (or sits on a branch): the images, ORIZON_VERSION,
    the env files and the running containers are not touched. Afterwards the
    new copy writes its deployment files (yours, if edited, are kept), and
    `orizonctl up` applies whatever changed in them.
    """
    template = cli_source or ctx.home_config().get("cli_source")
    if not template:
        raise click.ClickException("no CLI source recorded (install.sh writes it to $ORIZON_HOME/config.json); pass --cli-source")
    st = ctx.state
    ref = ref or (st.version if st and st.version else f"v{__version__}")
    spec = resolve_cli_spec(template, ref)
    ui.header(f"orizonctl {__version__} -> {ref}")
    # A republished tag carries the same package version, which a plain
    # --upgrade would call satisfied: install dependencies first, then force
    # the package itself (install.sh does the same).
    for flags in (["--upgrade"], ["--force-reinstall", "--no-deps", "--no-cache-dir"]):
        ui.step(f"pip install {' '.join(flags)} {spec}")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *flags, spec], capture_output=True, text=True)
        if r.returncode != 0:
            raise click.ClickException(f"pip failed, the installed orizonctl is unchanged or was restored by pip:\n{r.stderr.strip()[-1500:]}")
    if cli_source:
        ctx.save_home_config({**ctx.home_config(), "cli_source": cli_source})
    newbin = _self_bin()
    r = subprocess.run([newbin, "--version"], capture_output=True, text=True)
    ui.ok(f"installed: {(r.stdout or '').strip() or newbin}")
    if no_files:
        return
    # the new code's bundle, written by the new code
    code = subprocess.call([newbin, "--home", str(ctx.home), "--app-dir", str(ctx.app_dir), "init"])
    if code != 0:
        raise SystemExit(code)
    ui.note("containers were not touched; `orizonctl up` applies any change in the deployment files")


# --------------------------------------------------------------- service


def _now_in_release(ctx: Ctx, st: State) -> list[str]:
    """Added services that the compose files themselves define (a later release
    brought them): keeping both would merge two definitions of one service."""
    out = []
    for mcp in (False, True):
        names = [n for n, s in st.extra_services.items() if bool(s.get("mcp")) == mcp]
        if names and ctx.stack.has_file(mcp=mcp):
            defined = set(ctx.stack.every_service(mcp=mcp, pins=False))
            out += [n for n in names if n in defined]
    return out


def _set_env(app_dir: Path, f: EnvFile, values: dict[str, str], remove: set[str] | None = None) -> None:
    """Set (and drop) keys in one env file in place, comments kept."""
    target = app_dir / f.value
    base = (target if target.exists() else app_dir / TEMPLATES[f]).read_text(encoding="utf-8")
    text = _envfile.render(base, values) if values else base
    if remove:
        text = _envfile.drop(text, remove)
    _envfile.write_atomic(target, text, FILE_MODES[f])


def _connector_wiring(name: str, port: int) -> tuple[str, str, list[str]]:
    """What the backend and the connectors need to know about `mcp-<x>` on
    `port`: its URL key in .env, and its Host-header entries for .env.mcp."""
    key = f"MCP_{name[4:].upper().replace('-', '_')}_URL"
    return key, f"http://{name}:{port}/mcp", [f"{name}:{port}", f"localhost:{port}"]


def _wire_connector(ctx: Ctx, name: str, port: int) -> dict:
    """Returns what was added, so `service remove` takes back exactly that."""
    key, url, hosts = _connector_wiring(name, port)
    wired: dict = {}
    mcp_env = load(ctx.app_dir / EnvFile.MCP.value)
    if "MCP_ALLOWED_HOSTS" in mcp_env:
        allowed = [h.strip() for h in mcp_env["MCP_ALLOWED_HOSTS"].split(",") if h.strip()]
        new = [h for h in hosts if h not in allowed]
        if new:
            _set_env(ctx.app_dir, EnvFile.MCP, {"MCP_ALLOWED_HOSTS": ",".join(allowed + new)})
            wired["hosts"] = new
            ui.ok(f".env.mcp: MCP_ALLOWED_HOSTS += {', '.join(new)}")
    else:
        ui.warn(f".env.mcp has no MCP_ALLOWED_HOSTS; if the connector answers 421, add {', '.join(hosts)} to it")
    current = ctx.values.get(key)
    if current is None:
        _set_env(ctx.app_dir, EnvFile.ENV, {key: url})
        wired["url_key"] = key
        ui.ok(f".env: {key}={url}")
    elif current != url:
        ui.warn(f".env already has {key}={current}; left as is (the container listens at {url})")
    return wired


def _unwire_connector(ctx: Ctx, name: str, spec: dict) -> None:
    wired = spec.get("wired") or {}
    hosts = set(wired.get("hosts") or [])
    mcp_env = load(ctx.app_dir / EnvFile.MCP.value)
    if hosts and "MCP_ALLOWED_HOSTS" in mcp_env:
        kept = [h.strip() for h in mcp_env["MCP_ALLOWED_HOSTS"].split(",") if h.strip() and h.strip() not in hosts]
        _set_env(ctx.app_dir, EnvFile.MCP, {"MCP_ALLOWED_HOSTS": ",".join(kept)})
        ui.ok(f".env.mcp: MCP_ALLOWED_HOSTS -= {', '.join(sorted(hosts))}")
    key = wired.get("url_key")
    if key and spec.get("port") and ctx.values.get(key) == _connector_wiring(name, int(spec["port"]))[1]:
        _set_env(ctx.app_dir, EnvFile.ENV, {}, remove={key})
        ui.ok(f".env: {key} removed")


@main.group()
def service() -> None:
    """Containers this deployment runs beyond the release's compose files.

    \b
    `add` puts one in (for example a connector a later release ships, on a
    host that stays on the current one); `remove` takes it out; `list` shows
    them. They start and stop with the stack. To change the image of one, or
    of any release service: `orizonctl update SERVICE --image ...`.
    """


@service.command("add")
@click.argument("name")
@click.option("--image", help="Image reference to run. For mcp-<x> the default is <prefix>/orizon-mcp-<x> at --version.")
@click.option("--version", "tag", help="Tag of the default image (default: ORIZON_VERSION).")
@click.option("--port", type=click.IntRange(1, 65535), help="Port the container listens on. A connector gets it as MCP_PORT and is health-checked on it.")
@click.option("--publish", is_flag=True, help="Also publish --port on the host (default: reachable only inside orizon-network).")
@click.option("--env", "envs", multiple=True, metavar="KEY=VALUE", help="Environment variable for the container; repeatable.")
@click.option("--cred-vars", default="", help="Connectors: MCP_CRED_VARS, the variables a request's bearer token is forwarded as (e.g. ACCESS_TOKEN).")
@click.option("--stack", type=click.Choice(["main", "mcp"]), help="Compose stack to join (default: mcp for a name starting with mcp-, else main).")
@click.option("--no-wire", is_flag=True, help="Connectors: do not add it to MCP_ALLOWED_HOSTS (.env.mcp) or set MCP_<X>_URL (.env).")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_ctx
def service_add(ctx: Ctx, name: str, image: str | None, tag: str | None, port: int | None, publish: bool, envs: tuple[str, ...], cred_vars: str, stack: str | None, no_wire: bool, yes: bool) -> None:
    """Add a container the release's compose files do not define, and start it.

    \b
      orizonctl service add mcp-imap --port 8111
      orizonctl service add mcp-imap --port 8111 --version v1.4.0_dev
      orizonctl service add my-tool --image reg/team/tool:1 --port 9000 --env MODE=prod

    \b
    A connector (mcp-<x>, MCP stack) is set up like the bundled ones: .env.mcp,
    MCP_PORT, health check, and the wiring the backend needs (--no-wire skips
    it). Anything else joins orizon-network with the image, port and
    environment given here. Running `add` again for the same name replaces it.
    """
    st = ctx.require_state()
    if not _pins.valid_name(name):
        raise click.ClickException(f"not a service name: {name!r} (lowercase letters, digits, - and _)")
    mcp = (stack == "mcp") if stack else name.startswith("mcp-")
    if mcp and not (st.mcp and ctx.stack.has_file(mcp=True)):
        raise click.ClickException("the connector (MCP) stack is off on this deployment; turn it on in `orizonctl configure` first, or pass --stack main")
    for m in (False, True):
        if ctx.stack.has_file(mcp=m) and name in ctx.stack.every_service(mcp=m, pins=False):
            file = "docker-compose.mcp.yml" if m else "docker-compose.yml"
            raise click.ClickException(f"{name} is already a service of {file}. To run it on another image: orizonctl update {name} --image ...  (if it is not running, its compose profile is off: `orizonctl configure`)")
    if image and tag:
        raise click.ClickException("--image and --version are exclusive")
    if not image:
        if not name.startswith("mcp-"):
            raise click.ClickException("--image is needed (only mcp-<x> connectors have a default image name)")
        image = f"{ctx.values.get('ORIZON_IMAGE_PREFIX') or st.image_prefix}/orizon-{name}:{tag or st.version}"
    if not _pins.valid_ref(image):
        raise click.ClickException(f"not an image reference: {image!r}")
    env = _parse_sets(envs)
    wire = mcp and name.startswith("mcp-") and bool(port) and not no_wire
    previous = st.extra_services.get(name)

    ui.header(f"{'Replace' if previous else 'Add'} service {name}")
    ui.kv("image", image)
    ui.kv("stack", "MCP connectors (docker-compose.mcp.yml)" if mcp else "main (docker-compose.yml)")
    ui.kv("port", (f"{port}" + (" (published on the host)" if publish else " (orizon-network only)")) if port else "none")
    if env:
        ui.kv("environment", ", ".join(sorted(env)))
    if wire:
        key, url, hosts = _connector_wiring(name, port)
        ui.kv("wiring", f"{key}={url} in .env; {', '.join(hosts)} in MCP_ALLOWED_HOSTS")
    elif mcp and not port:
        ui.warn("no --port: the connector keeps its image's default port, gets no health check and is not wired to the backend")
    if not yes and _on_terminal() and not click.confirm("Continue?", default=True):
        raise click.Abort()

    try:
        ui.header("Image")
        _fetch_image(ctx, st, image)
        spec: dict = {"image": image, "mcp": mcp, "port": port, "publish": publish, "env": env, "cred_vars": cred_vars}
        if previous:
            _unwire_connector(ctx, name, previous)
        if wire:
            spec["wired"] = _wire_connector(ctx, name, port)
        st.extra_services[name] = spec
        st.save(ctx.app_dir)
        ctx.sync_pins()
        err = ctx.stack.validate_config(mcp=mcp)
        if err:
            raise click.ClickException(f"docker compose config rejects the service:\n{err}")
        ui.header("Start")
        ctx.stack.recreate([name], mcp=mcp)
        ok = ctx.stack.wait([name], APP_TIMEOUT, mcp=mcp, label=name)
    except ComposeError as e:
        ui.fail(e.format_message())
        ok = False
    if not ok:
        if name in (ctx.state.extra_services if ctx.state else {}):
            ui.note(f"the service is recorded; fix and re-run, or take it out: orizonctl service remove {name}")
        raise SystemExit(1)
    ui.header("Done")
    if port:
        ui.kv("Reachable at", f"http://{name}:{port}{'/mcp' if mcp else ''}  (from the stack's containers)")
    if wire and spec.get("wired", {}).get("url_key"):
        ui.kv("Backend", f"reads {spec['wired']['url_key']} when it starts: `orizonctl up` recreates it with the new value (or `orizonctl update backend ...` if it moves to a new image anyway)")
    if mcp:
        ui.kv("Panel", "a backend that does not know this connector can attach it under Settings > External MCP at the URL above")


@service.command("remove")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_ctx
def service_remove(ctx: Ctx, name: str, yes: bool) -> None:
    """Stop and remove a container added with `service add`."""
    st = ctx.require_state()
    spec = st.extra_services.get(name)
    if spec is None:
        raise click.ClickException(f"{name} was not added with `service add`" + (f" (added: {', '.join(sorted(st.extra_services))})" if st.extra_services else "; release services are turned off in `orizonctl configure`"))
    if not yes and _on_terminal():
        click.confirm(f"Stop and remove {name} ({spec.get('image')})?", default=True, abort=True)
    mcp = bool(spec.get("mcp"))
    if ctx.stack.has_file(mcp=mcp):
        ctx.stack.run("rm", "-sf", name, mcp=mcp, check=False)
    _unwire_connector(ctx, name, spec)
    st.extra_services.pop(name)
    st.save(ctx.app_dir)
    ctx.sync_pins()
    ui.ok(f"removed {name}")


@service.command("list")
@pass_ctx
def service_list(ctx: Ctx) -> None:
    """Added containers and pinned images."""
    st = ctx.require_state()
    if not (st.extra_services or st.image_pins or st.mcp_image_pins):
        ui.info("nothing added or pinned: every service is the release's, at ORIZON_VERSION")
        return
    for name, spec in sorted(st.extra_services.items()):
        ui.kv(name, f"added   {spec.get('image')}  [{'mcp' if spec.get('mcp') else 'main'}{', port ' + str(spec['port']) if spec.get('port') else ''}]")
    for name, ref in sorted({**st.image_pins, **st.mcp_image_pins}.items()):
        ui.kv(name, f"pinned  {ref}")


# -------------------------------------------------------------- rollback


@main.command()
@click.argument("snapshot", required=False)
@click.option("--db-only", is_flag=True, help="Only restore the database dump into the running db container; touch nothing else.")
@click.option("--no-db", is_flag=True, help="Keep the current database (only files, config and images go back).")
@click.option("--no-data", is_flag=True, help="Do not put the client data archive back (files created since the snapshot are never deleted anyway).")
@click.option("--no-pull", is_flag=True, help="Assume the previous images are still on this host.")
@click.option("--no-cli", "no_cli", is_flag=True, help="Keep this orizonctl even if the snapshot came from another release of it.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_ctx
def rollback(ctx: Ctx, snapshot: str | None, db_only: bool, no_db: bool, no_data: bool, no_pull: bool, no_cli: bool, yes: bool) -> None:
    """Return to a snapshot: the previous release's images, deployment files, env files and database.

    \b
    Without SNAPSHOT, the newest one `update` took (`orizonctl backup list`).
    In order: stack down; env files, state and deployment files restored;
    db started and the dump restored over it (--clean); client data put
    back; orizonctl moved to the snapshot's release; previous images pulled
    if missing; phased start; verify.
    """
    ctx.require_app()
    snap = bk.find_snapshot(ctx.home, snapshot)
    if snap is None:
        raise click.ClickException("no snapshot found" + (f" named {snapshot}" if snapshot else f" under {ctx.backups}; `orizonctl update` and `orizonctl backup` create them"))
    m = snap.manifest
    ui.header(f"Rollback to {snap.name}")
    ui.kv("taken", m.created_at)
    ui.kv("release", m.from_version + (f"  (before the update to {m.to_version})" if m.to_version else ""))
    ui.kv("contents", ", ".join(k for k in ("db", "env", "files", "data", "letsencrypt") if k in m.contents))
    if db_only and no_db:
        raise click.ClickException("--db-only and --no-db are exclusive")
    if not snap.has(bk.DB_DUMP) and not no_db:
        ui.warn("this snapshot holds no database dump; the current database stays (--no-db)")
        no_db = True
    if not yes:
        what = "the database only" if db_only else "the stack, its files" + ("" if no_db else " and the database")
        click.confirm(click.style(f"This replaces {what} with the snapshot's copy. Continue?", bold=True), default=False, abort=True)

    values = ctx.values
    try:
        if db_only:
            ui.header("Database")
            bk.restore_database(snap, ctx.stack, values)
            ui.ok("database restored; restart the application: orizonctl restart")
            return

        ui.header("Stopping the stack")
        if ctx.stack.has_file(mcp=True):
            ctx.stack.down(mcp=True)
        ctx.stack.down()

        ui.header("Files")
        restored = bk.restore_env(snap, ctx.app_dir)
        ui.ok(f"env files + state: {', '.join(restored)}")
        ctx.sync_pins()  # the snapshot's state decides which services are pinned
        n = bk.restore_files(snap, ctx.app_dir)
        ui.ok(f"deployment files: {n} restored")
        if not no_data:
            dirs = bk.restore_data(snap, ctx.app_dir)
            ui.ok(f"client data: {', '.join(dirs) or 'nothing archived'}")
        st = ctx.require_state()
        values = ctx.values

        if not no_db:
            ui.header("Database")
            ctx.stack.up(["db"])
            if not ctx.stack.wait(["db"], 180, label="database"):
                raise ComposeError("the database did not come up")
            bk.restore_database(snap, ctx.stack, values)
            ui.ok("database restored from the dump")

        if m.orizonctl_version and m.orizonctl_version != __version__ and not no_cli and m.from_version:
            template = m.cli_source or ctx.home_config().get("cli_source")
            if template:
                spec = resolve_cli_spec(template, m.from_version)
                ui.header(f"orizonctl {m.from_version}")
                ui.step(f"pip install {spec}")
                r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", spec], capture_output=True, text=True)
                if r.returncode != 0:
                    ui.warn(f"pip failed; keeping orizonctl {__version__}:\n{r.stderr.strip()[-800:]}")
                else:
                    newbin = _self_bin()
                    ui.ok(f"installed; starting the stack with {newbin}")
                    m.status = "rolled-back"
                    snap.save()
                    rest = [newbin, "--home", str(ctx.home), "--app-dir", str(ctx.app_dir), "up"]
                    if no_pull:
                        rest.append("--no-pull")
                    raise SystemExit(subprocess.call(rest))
            else:
                ui.warn("no CLI source recorded; keeping this orizonctl")

        ok = _up(ctx, pull=not no_pull, build=False, skip_preflight=False, download=False)
        if st.access_mode == "letsencrypt" and snap.has(bk.LETSENCRYPT_DIR):
            if bk.restore_letsencrypt(snap, ctx.stack):
                ctx.stack.restart(["nginx"])
                ui.ok("certificates restored")
    except ComposeError as e:
        raise click.ClickException(str(e))
    m.status = "rolled-back"
    snap.save()
    _summary(ctx, ok)
    if not ok:
        raise SystemExit(1)


# ----------------------------------------------------------------- config


@main.group()
def config() -> None:
    """The YAML config file: examples, and a check of yours before using it."""


@config.command("example")
@click.argument("name", required=False)
def config_example(name: str | None) -> None:
    """Print a bundled example config (NAME), or list the available ones.

    \b
      orizonctl config example                      # the list
      orizonctl config example reference            # every key, commented
      orizonctl config example gpu-letsencrypt > /root/orizon.yaml
    """
    names = example_names()
    if not names:
        raise click.ClickException("this orizonctl build carries no example configs")
    if name is None:
        for n in names:
            first = next((l[2:].strip() for l in example_text(n).splitlines() if l.startswith("# ") and l[2:].strip()), "")
            click.echo(f"  {n:<24} {first}")
        click.echo("\n  orizonctl config example <name> > orizon.yaml")
        return
    try:
        click.echo(example_text(name), nl=False)
    except ConfigError as e:
        raise click.ClickException(str(e))


@config.command("check")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def config_check(path: str) -> None:
    """Parse a config file and report what install.sh and the wizard will read from it.

    Unknown keys are listed (a typo otherwise falls back to the default and
    only shows up as a missing answer much later). Secrets are masked. To see
    the env files it would produce: `orizonctl configure --non-interactive
    --dry-run --config <file>`.
    """
    from .config import INSTALL_KEYS

    try:
        cfg = load_config(Path(path))
    except ConfigError as e:
        raise click.ClickException(str(e))
    ui.header(f"install: ({len(cfg.install)} keys, read by install.sh)")
    for k, v in cfg.install.items():
        ui.kv(k, f"{v}   -> {INSTALL_KEYS[k]}" if k in INSTALL_KEYS else str(v))
    ui.header(f"configure: ({len(cfg.configure)} sections, read by the wizard)")

    def show(node, prefix: str = "") -> None:
        for k, v in node.items():
            if isinstance(v, dict):
                show(v, f"{prefix}{k}.")
            else:
                shown = ui.mask(str(v)) if is_secret_name(k) or k in ("token", "password", "cloudflare_token", "api_key", "api_hash", "bot_token", "hf_token", "master_key") else str(v)
                ui.kv(f"{prefix}{k}", shown, width=36)

    show(cfg.configure)
    click.echo("")
    if cfg.unknown:
        cfg.warn_unknown()
        raise SystemExit(1)
    ui.ok("no unknown keys")


# -------------------------------------------------------------------- env


@main.group()
def env() -> None:
    """Inspect the generated env files."""


@env.command("show")
@click.option("--reveal", is_flag=True, help="Print secrets in clear.")
@pass_ctx
def env_show(ctx: Ctx, reveal: bool) -> None:
    """Effective configuration per file, secrets masked."""
    ctx.require_app()
    for f in EnvFile:
        p = ctx.app_dir / f.value
        if not p.exists():
            continue
        ui.step(f"{f.value}  ({oct(p.stat().st_mode & 0o777)})")
        for k, v in load(p).items():
            ui.kv(k, v if (reveal or not is_secret_name(k)) else ui.mask(v), width=40)


@env.command("diff")
@pass_ctx
def env_diff(ctx: Ctx) -> None:
    """Keys the shipped templates declare that your files lack."""
    ctx.require_app()
    any_missing = False
    for f in available_files(ctx.app_dir):
        target = ctx.app_dir / f.value
        if not target.exists():
            ui.warn(f"{f.value}: not present")
            continue
        missing = missing_keys((ctx.app_dir / TEMPLATES[f]).read_text(), target.read_text())
        if missing:
            any_missing = True
            ui.warn(f"{f.value}: missing {len(missing)} template keys")
            for k in missing:
                click.echo(f"      {k}")
        else:
            ui.ok(f"{f.value}: has every active template key")
    if not any_missing:
        ui.ok("nothing to add")


# ----------------------------------------------------------------- models


@main.group()
def models() -> None:
    """Model weights under MODELS_PATH."""


@models.command("list")
@pass_ctx
def models_list(ctx: Ctx) -> None:
    """Which models this deployment needs and whether they are on disk."""
    from .models import catalog, present

    st = ctx.require_state()
    values = ctx.values
    mp = Path(values.get("MODELS_PATH", "/srv/orizon-models"))
    for it in catalog(st, values):
        (ui.ok if present(mp, it) else ui.warn)(f"{it.role:<14} {it.repo_id:<40} -> {mp / it.local_dir}")


@models.command("download")
@click.option("--token", help="HuggingFace token (default: HUGGINGFACE_TOKEN from .env.secrets).")
@click.option("--force", is_flag=True, help="Re-download even if the directory exists.")
@click.option("--only", multiple=True, help="Role(s) to download: text, embedding, rerank, transcription, diarization.")
@pass_ctx
def models_download(ctx: Ctx, token: str | None, force: bool, only: tuple[str, ...]) -> None:
    """Download the configured models from HuggingFace."""
    from .models import catalog, download

    st = ctx.require_state()
    values = ctx.values
    items = catalog(st, values, download=True)
    if only:
        items = [i for i in items if i.role in only]
    if not items:
        ui.info("nothing to download for this configuration")
        return
    ok = download(Path(values["MODELS_PATH"]), items, token or values.get("HUGGINGFACE_TOKEN"), force=force)
    if not ok:
        raise SystemExit(1)


# -------------------------------------------------------------------- ssl


@main.group()
def ssl() -> None:
    """TLS certificate operations."""


@ssl.command("issue")
@pass_ctx
def ssl_issue(ctx: Ctx) -> None:
    """Obtain / renew the Let's Encrypt certificate via setup-ssl.sh and switch nginx to HTTPS."""
    st = ctx.require_state()
    if st.access_mode != "letsencrypt":
        raise click.ClickException(f"access mode is {st.access_mode}; `ssl issue` applies to the Let's Encrypt mode only")
    if not _run_setup_ssl(ctx):
        raise SystemExit(1)


@ssl.command("status")
@pass_ctx
def ssl_status(ctx: Ctx) -> None:
    """Expiry of the certificate currently served."""
    from .doctor import cert_expiry

    st = ctx.require_state()
    if not st.https:
        ui.info("this deployment does not serve HTTPS")
        return
    exp = cert_expiry(st.domain)
    if exp is None:
        raise click.ClickException(f"could not read the certificate served at {st.domain}:443")
    ui.kv("domain", st.domain)
    ui.kv("expires", exp.isoformat())
    ui.kv("template", ctx.values.get("NGINX_TEMPLATE", ""))
    ui.kv("staging", ctx.values.get("CERTBOT_STAGING", "n/a"))


# ---------------------------------------------------------------- systemd


@main.group()
def systemd() -> None:
    """Host systemd units (log cleanup timer)."""


@systemd.command("install")
@click.option("--max-container-log-mb", default=200, show_default=True)
@click.option("--retention-days", default=14, show_default=True)
@pass_ctx
def systemd_install(ctx: Ctx, max_container_log_mb: int, retention_days: int) -> None:
    """Install and enable the daily log cleanup timer."""
    from . import systemd as sd

    ctx.require_app()
    if not sd.install(ctx.app_dir, max_container_log_mb, retention_days):
        raise SystemExit(1)


@systemd.command("remove")
def systemd_remove() -> None:
    """Disable and delete the log cleanup units."""
    from . import systemd as sd

    sd.remove()


@systemd.command("status")
def systemd_status() -> None:
    from . import systemd as sd

    click.echo(sd.status())


# ---------------------------------------------------------------- info


@main.command()
@pass_ctx
def info(ctx: Ctx) -> None:
    """Where things are and what is configured."""
    st = ctx.state
    ui.kv("orizonctl", f"{__version__}  ({'bundled deployment files' if is_bundle_available() else 'no bundle: source checkout mode'})")
    ui.kv("home", str(ctx.home))
    ui.kv("app dir", f"{ctx.app_dir}" + ("" if app_dir_is_materialized(ctx.app_dir) else "  (empty: run orizonctl init)"))
    cfg = ctx.home_config()
    if cfg.get("cli_source"):
        ui.kv("cli source", cfg["cli_source"])
    if st:
        ui.kv("ORIZON_VERSION", st.version)
        ui.kv("images", f"{st.image_prefix}/*  (registry {st.registry_host}{', login' if st.registry_login else ''})")
        ui.kv("configured", st.configured_at)
        ui.kv("access", f"{st.access_mode} -> {st.public_url}")
        ui.kv("models", f"text={st.text_mode} embedding={st.embedding_mode} rerank={st.rerank_mode} transcription={'on' if st.transcription else 'off'}")
        ui.kv("mcp", ("on" + (f" (profiles: {', '.join(st.mcp_profiles)})" if st.mcp_profiles else "")) if st.mcp else "off")
        ui.kv("services", ", ".join(st.all_services()))
        for s, ref in sorted({**st.image_pins, **st.mcp_image_pins}.items()):
            ui.kv(f"pinned: {s}", ref)
        for s, spec in sorted(st.extra_services.items()):
            ui.kv(f"added: {s}", str(spec.get("image")))
    else:
        ui.kv("configured", "no (run orizonctl setup)")
    for f in EnvFile:
        p = ctx.app_dir / f.value
        ui.kv(f.value, f"{oct(p.stat().st_mode & 0o777)} {len(load(p))} keys" if p.exists() else "absent")


if __name__ == "__main__":  # pragma: no cover
    main()
