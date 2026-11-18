"""Docker Compose control for the two stacks (main + MCP).

Startup is phased and every phase passes `--no-deps`, because the compose file
declares dependencies the deployment does not always want: the embedding and
rerank servers `depends_on` the text server, so on a host that serves text
from an external API `docker compose up embedding-vllm-server` would drag a
35B model in. Ordering and health waits are done here instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

import click

from . import ui
from .schema import MCP_PROJECT_NAME, PROJECT_NAME
from .state import State

MAIN_FILE = "docker-compose.yml"
MCP_FILE = "docker-compose.mcp.yml"

# vLLM loads tens of GB of weights and the compose healthcheck gives it a 300s
# start period; a cold first boot on slow disk can take longer still.
MODEL_TIMEOUT = 1800
APP_TIMEOUT = 600


class ComposeError(click.ClickException):
    pass


class Stack:
    def __init__(self, repo: Path, runtime: str = "docker"):
        self.repo = repo
        self.runtime = runtime

    # -------------------------------------------------------------- plumbing

    def _base(self, mcp: bool) -> list[str]:
        file = MCP_FILE if mcp else MAIN_FILE
        project = MCP_PROJECT_NAME if mcp else PROJECT_NAME
        return [self.runtime, "compose", "-p", project, "-f", str(self.repo / file), "--project-directory", str(self.repo)]

    def run(self, *args: str, mcp: bool = False, check: bool = True, capture: bool = False, timeout: int | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """`env` adds variables for Compose's interpolation; a variable set in
        the environment beats the same name in .env, which is how images at a
        tag other than the one in .env are pulled."""
        cmd = self._base(mcp) + list(args)
        full_env = {**os.environ, **env} if env else None
        try:
            r = subprocess.run(cmd, cwd=str(self.repo), text=True, capture_output=capture, timeout=timeout, env=full_env)
        except FileNotFoundError:
            raise ComposeError(f"{self.runtime} is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            raise ComposeError(f"timed out: {' '.join(cmd)}")
        if check and r.returncode != 0:
            detail = (r.stderr or "").strip() if capture else ""
            raise ComposeError(f"command failed ({r.returncode}): {' '.join(cmd)}" + (f"\n{detail}" if detail else ""))
        return r

    def docker(self, *args: str, check: bool = True, timeout: int | None = 120) -> subprocess.CompletedProcess:
        try:
            r = subprocess.run([self.runtime, *args], text=True, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise ComposeError(f"{self.runtime} is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            raise ComposeError(f"timed out: {self.runtime} {' '.join(args)}")
        if check and r.returncode != 0:
            raise ComposeError(f"{self.runtime} {' '.join(args)} failed: {r.stderr.strip()}")
        return r

    def has_file(self, mcp: bool = False) -> bool:
        return (self.repo / (MCP_FILE if mcp else MAIN_FILE)).exists()

    def config_services(self, mcp: bool = False) -> list[str]:
        r = self.run("config", "--services", mcp=mcp, capture=True)
        return [s for s in r.stdout.split() if s]

    def every_service(self, mcp: bool = False) -> list[str]:
        """Every service in the file, whichever profile it is under; `config_services`
        lists only those the active COMPOSE_PROFILES start."""
        r = self.run("--profile", "*", "config", "--services", mcp=mcp, check=False, capture=True)
        return [s for s in r.stdout.split() if s]

    def remove_inactive(self, mcp: bool = False) -> list[str]:
        """Stop and remove the containers of services no active profile starts any more.

        Taking a profile out of COMPOSE_PROFILES does not do it: `up -d
        --remove-orphans` treats a service under an inactive profile as part of the
        project, not an orphan, so its container would keep running. Naming the
        service to `ps` and `rm` works whatever the profiles are.
        """
        active = set(self.config_services(mcp=mcp))
        stale = [s for s in self.every_service(mcp=mcp) if s not in active and self.container_ids(s, mcp=mcp)]
        if stale:
            self.run("rm", "-sf", *stale, mcp=mcp, check=False)
        return stale

    def validate_config(self, mcp: bool = False) -> str | None:
        """Compose's own parse of the file with the current env files: catches
        a missing NGINX_TEMPLATE file or a malformed value before anything starts."""
        r = self.run("config", "--quiet", mcp=mcp, check=False, capture=True)
        if r.returncode != 0:
            return (r.stderr or r.stdout).strip()
        return None

    def container_ids(self, service: str, mcp: bool = False) -> list[str]:
        r = self.run("ps", "-aq", service, mcp=mcp, check=False, capture=True)
        return [c for c in r.stdout.split() if c]

    def inspect_state(self, cid: str) -> tuple[str, str, int]:
        r = self.docker("inspect", "-f", "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.ExitCode}}", cid, check=False)
        if r.returncode != 0:
            return "missing", "none", -1
        status, health, code = (r.stdout.strip().split("|") + ["", "", "0"])[:3]
        try:
            return status, health, int(code)
        except ValueError:
            return status, health, 0

    def service_status(self, service: str, mcp: bool = False) -> str:
        """ready | starting | failed:<why> | absent"""
        ids = self.container_ids(service, mcp)
        if not ids:
            return "absent"
        for cid in ids:
            status, health, code = self.inspect_state(cid)
            if status in ("exited", "dead"):
                # one-shot helpers (sandbox-image-loader) exit 0 by design
                return "ready" if code == 0 and service == "sandbox-image-loader" else f"failed:{status} (exit {code})"
            if status != "running":
                return "starting"
            if health == "unhealthy":
                return "failed:unhealthy"
            if health == "starting":
                return "starting"
        return "ready"

    # ------------------------------------------------------------ operations

    def pull(self, services: list[str], mcp: bool = False, env: dict[str, str] | None = None) -> None:
        if not services and not mcp:
            return
        # Every service here also carries a `build:` block, so no
        # --ignore-buildable: that would skip them all. `pull` fetches by the
        # `image:` name regardless of the build context.
        self.run("pull", *([] if mcp else services), mcp=mcp, env=env)

    def up(self, services: list[str], mcp: bool = False, build: bool = False, no_deps: bool = True) -> None:
        if not services and not mcp:
            return
        args = ["up", "-d"]
        if mcp:
            # the MCP file is brought up whole, so orphans are safe to drop
            args.append("--remove-orphans")
        elif no_deps:
            args.append("--no-deps")
        args.append("--build" if build else "--no-build")
        if not mcp:
            args += services
        self.run(*args, mcp=mcp)

    def down(self, mcp: bool = False, volumes: bool = False) -> None:
        args = ["down", "--remove-orphans"]
        if volumes:
            args.append("-v")
        self.run(*args, mcp=mcp, check=False)

    def stop(self, services: list[str], mcp: bool = False) -> None:
        self.run("stop", *services, mcp=mcp, check=False)

    def restart(self, services: list[str], mcp: bool = False) -> None:
        self.run("restart", *services, mcp=mcp)

    def ps(self, mcp: bool = False) -> None:
        self.run("ps", "--format", "table {{.Name}}\t{{.Service}}\t{{.Status}}\t{{.Ports}}", mcp=mcp, check=False)

    def logs(self, services: list[str], follow: bool, tail: int, mcp: bool = False) -> None:
        args = ["logs", f"--tail={tail}"]
        if follow:
            args.append("-f")
        try:
            self.run(*args, *services, mcp=mcp, check=False)
        except KeyboardInterrupt:
            pass

    def exec(self, service: str, cmd: list[str], mcp: bool = False, capture: bool = True, stdin: bytes | None = None, timeout: int | None = None, stdin_file=None, stdout=None) -> subprocess.CompletedProcess:
        """`stdin_file` / `stdout` stream a dump in or out of the container
        without holding it in memory (with `stdout`, stderr is still captured)."""
        full = self._base(mcp) + ["exec", "-T", service, *cmd]
        kw: dict = {"cwd": str(self.repo), "timeout": timeout}
        if stdin_file is not None:
            kw["stdin"] = stdin_file
        else:
            kw["input"] = stdin
        if stdout is not None:
            kw["stdout"] = stdout
            kw["stderr"] = subprocess.PIPE
        else:
            kw["capture_output"] = capture
        try:
            return subprocess.run(full, **kw)
        except subprocess.TimeoutExpired:
            raise ComposeError(f"timed out: exec {service} {' '.join(cmd)}")

    def copy_from_container(self, cid: str, src: str, dest: Path) -> bool:
        """`docker cp <container>:<src> <dest>`; False when it fails."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self.docker("cp", f"{cid}:{src}", str(dest), check=False, timeout=600)
        return r.returncode == 0

    def copy_to_container(self, src: Path, cid: str, dest: str) -> bool:
        r = self.docker("cp", f"{src}/.", f"{cid}:{dest}", check=False, timeout=600)
        return r.returncode == 0

    def tail(self, service: str, lines: int = 60, mcp: bool = False) -> str:
        r = self.run("logs", "--no-color", f"--tail={lines}", service, mcp=mcp, check=False, capture=True)
        return (r.stdout or "") + (r.stderr or "")

    # ---------------------------------------------------------------- waits

    def wait(self, services: list[str], timeout: int, mcp: bool = False, label: str = "") -> bool:
        """Poll until every service is ready. On a failure, print its last log
        lines and return False; on timeout, print every laggard's logs."""
        if not services:
            return True
        start = time.monotonic()
        pending = list(services)
        last_report = 0.0
        while pending:
            for s in list(pending):
                st = self.service_status(s, mcp)
                if st == "ready":
                    ui.ok(f"{s} ready")
                    pending.remove(s)
                elif st == "absent":
                    ui.warn(f"{s}: no container (not in this compose file?)")
                    pending.remove(s)
                elif st.startswith("failed:"):
                    ui.fail(f"{s} {st[7:]}")
                    click.echo(_indent(self.tail(s, 80, mcp)))
                    return False
            if not pending:
                break
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                ui.fail(f"timeout after {int(elapsed)}s waiting for: {', '.join(pending)}")
                for s in pending:
                    click.echo(click.style(f"--- last log lines: {s} ---", bold=True))
                    click.echo(_indent(self.tail(s, 40, mcp)))
                return False
            if elapsed - last_report >= 30:
                ui.info(f"{label or 'waiting'}: {', '.join(pending)} still starting ({int(elapsed)}s)")
                last_report = elapsed
            time.sleep(5)
        return True

    # ------------------------------------------------------------ bring-up

    def bring_up(self, state: State, values: dict[str, str], pull: bool = True, build: bool = False, defer_model_wait: bool = False) -> bool:
        """Start everything in order. With `defer_model_wait`, the (long) wait
        for the model servers is left to the caller via `self.pending_models`,
        so that certificate issuance is not held up by weights loading."""
        self.pending_models: list[str] = []
        available = set(self.config_services())
        wanted = state.all_services()
        missing = [s for s in wanted if s not in available]
        for s, (why, fix) in self.explain_missing(missing).items():
            ui.fail(f"{s}: {why}; it cannot start")
            ui.note(fix)
        wanted = [s for s in wanted if s in available]

        if pull and not build:
            ui.step(f"Pulling images for ORIZON_VERSION={values.get('ORIZON_VERSION', '?')}")
            self.pull(wanted)
            if state.mcp and self.has_file(mcp=True):
                self.pull([], mcp=True)

        ui.step("Starting the database")
        self.up([s for s in ["db"] if s in available], build=build)
        if not self.wait(["db"], 180, label="database"):
            return False
        self.sync_db_password(values)

        models = [s for s in state.model_services() if s in available]
        if models:
            ui.step(f"Starting model servers: {', '.join(models)}")
            self.up(models, build=build)

        apps = [s for s in state.app_services() if s in available]
        ui.step(f"Starting the application: {', '.join(apps)}")
        self.up(apps, build=build)

        edge = [s for s in state.edge_services() if s in available]
        if state.access_mode == "letsencrypt":
            # nginx first, on whatever template .env holds; setup-ssl.sh issues the
            # certificate, flips the template and starts certbot itself.
            if "nginx" in edge:
                ui.step("Starting nginx")
                self.up(["nginx"], build=build)
            edge = [s for s in edge if s == "certbot" and values.get("NGINX_TEMPLATE") == "orizonai.conf.template"]
            if edge:
                self.up(edge, build=build)
        elif edge:
            ui.step(f"Starting the edge: {', '.join(edge)}")
            self.up(edge, build=build)

        ok = True
        ui.step("Waiting for the application")
        ok &= self.wait(apps + [s for s in ["nginx"] if s in wanted], APP_TIMEOUT, label="application")
        ok &= self._wait_http(values, state)
        if models and defer_model_wait:
            self.pending_models = models
        elif models:
            ok &= self.wait_models(models)

        if state.mcp and self.has_file(mcp=True):
            ui.step("Starting the MCP connector stack")
            self.up([], mcp=True, build=build)
            mcp_services = [s for s in self.config_services(mcp=True) if s != "sandbox-image-loader"]
            ok &= self.wait(mcp_services, 600, mcp=True, label="connectors")

        for mcp in (False, True):
            if mcp and not (state.mcp and self.has_file(mcp=True)):
                continue
            stale = self.remove_inactive(mcp=mcp)
            if stale:
                ui.ok(f"removed, their profile is off: {', '.join(stale)}")
        if missing:
            ui.fail(f"not started, see above: {', '.join(missing)}")
        return bool(ok) and not missing

    def explain_missing(self, services: list[str], mcp: bool = False) -> dict[str, tuple[str, str]]:
        """Why each service is not part of the active stack, and the fix."""
        if not services:
            return {}
        defined = set(self.every_service(mcp=mcp))
        file = MCP_FILE if mcp else MAIN_FILE
        out: dict[str, tuple[str, str]] = {}
        for s in services:
            if s in defined:
                out[s] = ("its compose profile is off", "COMPOSE_PROFILES in .env does not enable it. Re-run `orizonctl configure`.")
            else:
                out[s] = (
                    f"not defined in {file}",
                    f"{file} is older than this orizonctl, usually a copy kept as a local edit. "
                    "`orizonctl init --force` replaces it and saves yours under .orizon/local-edits/.",
                )
        return out

    def sync_db_password(self, values: dict[str, str]) -> bool:
        """Postgres reads POSTGRES_PASSWORD only when it creates its volume, so a
        password changed later in .env never reaches a database that already
        exists. Set the role's password to the one the backend logs in with."""
        user, password = db_credentials(values)
        if not password:
            return True
        sql = f"ALTER ROLE {_sql_ident(user)} WITH PASSWORD {_sql_literal(password)};\n"
        r = self.exec("db", ["psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", user, "-d", "postgres"], stdin=sql.encode(), timeout=60)
        if r.returncode != 0:
            lines = (r.stderr or b"").decode(errors="replace").strip().splitlines()
            ui.warn(f"could not set the database password from .env: {lines[-1] if lines else 'psql failed'}")
            ui.note("If the database volume was created with another password, the backend fails with P1000 (authentication failed).")
            return False
        ui.ok(f"database role {user} uses the password from .env")
        return True

    def wait_models(self, models: list[str]) -> bool:
        if not models:
            return True
        ui.step("Waiting for the model servers (first boot loads weights from disk; this can take many minutes)")
        return self.wait(models, MODEL_TIMEOUT, label="models")

    def _wait_http(self, values: dict[str, str], state: State, timeout: int = 300) -> bool:
        backend = f"http://{values.get('SERVER_BIND_ADDRESS') or '127.0.0.1'}:{values.get('SERVER_PORT', '8005')}/health"
        frontend = f"http://127.0.0.1:{values.get('FRONTEND_PORT', '3000')}/"
        targets = {"backend /health": backend, "frontend": frontend}
        if state.uses_nginx:
            targets["nginx /nginx-health"] = f"http://127.0.0.1:{values.get('NGINX_HTTP_PORT') or 80}/nginx-health"
        start = time.monotonic()
        pending = dict(targets)
        while pending and time.monotonic() - start < timeout:
            for name, url in list(pending.items()):
                if http_ok(url):
                    ui.ok(f"{name} answers")
                    pending.pop(name)
            if pending:
                time.sleep(5)
        for name, url in pending.items():
            ui.fail(f"{name} did not answer at {url}")
        return not pending


def http_ok(url: str, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (loopback)
            return 200 <= r.status < 400
    except urllib.error.HTTPError as e:
        return e.code < 500 and e.code not in (502, 503, 504)
    except Exception:
        return False


def db_credentials(values: dict[str, str]) -> tuple[str, str]:
    """The role and password the backend logs in with: DATABASE_URL when it
    points at the bundled `db`, otherwise POSTGRES_USER / POSTGRES_PASSWORD."""
    url = urlsplit(values.get("DATABASE_URL") or "")
    if url.hostname == "db" and url.username and url.password:
        return unquote(url.username), unquote(url.password)
    return values.get("POSTGRES_USER") or "postgres", values.get("POSTGRES_PASSWORD") or ""


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _indent(text: str, prefix: str = "    | ") -> str:
    return "\n".join(prefix + l for l in text.rstrip().splitlines()[-80:])


def ensure_dirs(repo: Path, state: State, values: dict[str, str]) -> None:
    """The bind-mount sources compose expects to exist as directories."""
    dirs = [repo / "backend-orizon-ai" / "data", repo / "backend-orizon-ai" / "logs", repo / "certs"]
    if state.transcription:
        dirs += [repo / "transcription-bot-orizon-ai" / "data", repo / "transcription-bot-orizon-ai" / "logs"]
    if values.get("MODELS_PATH") and state.keeps_models:
        dirs.append(Path(values["MODELS_PATH"]))
    if values.get("ORIZON_SMB_MOUNT_BASE_DIR"):
        dirs.append(Path(values["ORIZON_SMB_MOUNT_BASE_DIR"]))
    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            ui.warn(f"could not create {d}: {e}")


def check_custom_cert(repo: Path, values: dict[str, str]) -> str | None:
    raw = values.get("SSL_CERT_DIR") or "./certs"
    d = Path(raw) if raw.startswith("/") else repo / raw
    missing = [n for n in ("fullchain.pem", "privkey.pem") if not (d / n).exists()]
    if missing:
        return f"{d}: missing {', '.join(missing)}"
    return None


def registry_login(host: str, username: str, token: str, runtime: str = "docker") -> tuple[bool, str]:
    """`docker login` with the token on stdin so it never appears in a process
    list. Docker keeps the credential in /root/.docker/config.json; orizonctl
    stores nothing itself."""
    target = [] if host in ("docker.io", "index.docker.io", "") else [host]
    try:
        r = subprocess.run([runtime, "login", *target, "-u", username, "--password-stdin"], input=token, text=True, capture_output=True, timeout=60)
    except FileNotFoundError:
        return False, f"{runtime} is not installed"
    except subprocess.TimeoutExpired:
        return False, "docker login timed out"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "login failed"
    return True, "login succeeded"


def image_accessible(image_ref: str, runtime: str = "docker") -> tuple[bool, str]:
    """Can this host pull `image_ref`? `manifest inspect` answers without
    downloading layers and fails fast on a missing login or a wrong tag."""
    try:
        r = subprocess.run([runtime, "manifest", "inspect", image_ref], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, f"{runtime} is not installed"
    except subprocess.TimeoutExpired:
        return False, "registry did not answer in time"
    if r.returncode == 0:
        return True, "manifest found"
    msg = (r.stderr or r.stdout).strip().splitlines()
    return False, msg[-1] if msg else "manifest inspect failed"
