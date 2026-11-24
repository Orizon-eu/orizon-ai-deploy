"""update / rollback / backup: the file and database sides, with Docker stubbed.

A configured CPU-only deployment is written into `repo`, a fake Stack records
what would be run, then `update --latest` / `--version` and `rollback` are
exercised end to end through the CLI.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from orizonctl import backup as bk
from orizonctl import cli, hostinfo, migrate as mig
from orizonctl import registry
from orizonctl.cli import latest_release, main
from orizonctl.compose import Stack
from orizonctl.envfile import load
from orizonctl.schema import EnvFile
from orizonctl.state import State

ANSWERS = {
    "version": "v1.2.0",
    "registry": {"prefix": "example.com/orizon", "login": False},
    "models": {"text": "external", "embedding": "external", "rerank": "off"},
    "external": {"text": {"url": "http://llm:8000", "model": "m"}, "embedding": {"url": "http://emb:8000", "model": "e"}},
    "access": {"mode": "direct", "public_url": "http://10.0.0.5:3000"},
}

DUMP = b"PGDMP fake dump bytes"


class FakeStack(Stack):
    """No docker: records calls, answers a ready db, streams a fake dump."""

    def __init__(self, repo: Path, runtime: str = "docker"):
        super().__init__(repo, runtime)
        self.calls: list[tuple] = []
        self.restored: bytes | None = None

    def run(self, *args, mcp=False, check=True, capture=False, timeout=None, env=None, pins=True):
        self.calls.append(("run", args, mcp, env))
        import subprocess

        out = "db\nfrontend\nbackend\n" if args[:2] == ("config", "--services") else ""
        if args[:3] == ("config", "--format", "json"):
            out = json.dumps({"services": {s: {"image": i} for s, i in self.images(env, pins).items()}})
        return subprocess.CompletedProcess(args, 0, out, "")

    def images(self, env, pins):
        """What Compose would resolve: the release tag, then the pin file on top."""
        tag = (env or {}).get("ORIZON_VERSION") or load(self.repo / ".env")["ORIZON_VERSION"]
        out = {"db": "postgres:15-alpine", "frontend": f"example.com/orizon/orizon-ai-frontend:{tag}", "backend": f"example.com/orizon/orizon-ai-backend:{tag}"}
        st = State.load(self.repo)
        if pins and st and (self.repo / ".orizon" / "overrides.yml").exists():
            out.update(st.image_pins)
            out.update({n: s["image"] for n, s in st.extra_services.items() if not s.get("mcp")})
        return out

    def pull_image(self, ref):
        self.calls.append(("pull_image", ref))

    def _wait_http(self, values, state, timeout=300):
        return True

    def service_status(self, service, mcp=False):
        return "ready"

    def container_ids(self, service, mcp=False):
        return []

    def exec(self, service, cmd, mcp=False, capture=True, stdin=None, timeout=None, stdin_file=None, stdout=None):
        import subprocess

        self.calls.append(("exec", service, tuple(cmd)))
        if cmd[0] == "pg_dump":
            stdout.write(DUMP)
        if cmd[0] == "pg_restore":
            self.restored = stdin_file.read()
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def wait(self, services, timeout, mcp=False, label=""):
        return True

    def validate_config(self, mcp=False):
        return None


@pytest.fixture
def deployed(repo, tmp_path, monkeypatch):
    """A configured CPU-only deployment under `repo` with a fake stack, and a
    home directory for backups."""
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"cli_source": "/nonexistent/{version}"}))
    stacks: list[FakeStack] = []

    def make_stack(app_dir, runtime="docker"):
        s = FakeStack(app_dir, runtime)
        stacks.append(s)
        return s

    monkeypatch.setattr(cli, "Stack", make_stack)
    monkeypatch.setattr(cli, "_up", lambda ctx, pull, build, skip_preflight, download: True)
    monkeypatch.setattr(cli, "image_accessible", lambda ref: (True, "stubbed"))
    monkeypatch.setattr(cli, "apply_host_fixes", lambda *a, **k: None)
    cfg = tmp_path / "a.json"
    cfg.write_text(json.dumps(ANSWERS))
    r = CliRunner().invoke(main, ["--home", str(home), "--app-dir", str(repo), "configure", "--non-interactive", "--config", str(cfg)])
    assert r.exit_code == 0, r.output
    # some client data to carry
    (repo / "backend-orizon-ai" / "data").mkdir(parents=True)
    (repo / "backend-orizon-ai" / "data" / "upload.bin").write_bytes(b"client bytes")
    (repo / "backend-orizon-ai" / "logs").mkdir(parents=True)
    (repo / "backend-orizon-ai" / "logs" / "app.log").write_text("not backed up")
    return {"repo": repo, "home": home, "stacks": stacks}


def invoke(d, *args, input=None):
    return CliRunner().invoke(main, ["--home", str(d["home"]), "--app-dir", str(d["repo"]), *args], input=input, catch_exceptions=False)


# ---------------------------------------------------------------- backup


def test_backup_snapshot_layout_and_list(deployed):
    d = deployed
    r = invoke(d, "backup")
    assert r.exit_code == 0, r.output
    snaps = bk.list_snapshots(d["home"])
    assert len(snaps) == 1
    s = snaps[0]
    assert s.manifest.kind == "manual" and s.manifest.from_version == "v1.2.0"
    assert (s.dir / bk.DB_DUMP).read_bytes() == DUMP
    assert (s.dir / bk.DB_DUMP).stat().st_mode & 0o777 == 0o600
    assert (s.dir / bk.ENV_DIR / ".env").exists() and (s.dir / bk.ENV_DIR / ".env.secrets").exists() and (s.dir / bk.ENV_DIR / "state.json").exists()
    with tarfile.open(s.dir / bk.FILES_TAR) as tar:
        names = tar.getnames()
    assert "docker-compose.yml" in names and "env.example" in names
    with tarfile.open(s.dir / bk.DATA_TAR) as tar:
        names = tar.getnames()
    assert "backend-orizon-ai/data/upload.bin" in names
    assert not any("logs" in n for n in names)
    assert set(s.manifest.contents) == {"env", "files", "db", "data"}

    r = invoke(d, "backup", "list")
    assert r.exit_code == 0 and s.name in r.output and "manual" in r.output

    r = invoke(d, "backup", "--no-data", "--no-db")
    assert r.exit_code == 0, r.output
    s2 = bk.list_snapshots(d["home"])[-1]
    assert not s2.has(bk.DB_DUMP) and not s2.has(bk.DATA_TAR)

    r = invoke(d, "backup", "prune", "--keep", "1", "--yes")
    assert r.exit_code == 0, r.output
    assert [x.name for x in bk.list_snapshots(d["home"])] == [s2.name]


# --------------------------------------------------------------- migrate


def test_env_migration_plan_and_apply(tmp_path):
    old_t = "# app\n# Where the API lives\nAPI_URL=http://old\nKEEP=1\nLIMIT=10\n# OPT=\n"
    new_t = "# app\n# Where the API lives\nAPI_URL=http://old\nKEEP=1\n# Requests per minute; raise on big hosts\nLIMIT=20\n# Brand new switch\n# (two lines of help)\nFEATURE_X=false\n# OPT=\n"
    current = "API_URL=http://mine\nKEEP=1\nLIMIT=10\nOPT=set\nGONE=bye\nMINE=hand-added\n"
    old_t += "GONE=1\n"
    plan = mig.plan_file(EnvFile.ENV, new_t, current, old_t)
    kinds = {c.key: c.kind for c in plan.changes}
    assert kinds == {"FEATURE_X": mig.NEW, "LIMIT": mig.CHANGED_DEFAULT, "GONE": mig.REMOVED, "MINE": mig.EXTRA}
    fx = next(c for c in plan.changes if c.key == "FEATURE_X")
    assert fx.new_default == "false" and fx.help == "Brand new switch\n(two lines of help)"
    lim = next(c for c in plan.changes if c.key == "LIMIT")
    assert (lim.current, lim.old_default, lim.new_default) == ("10", "10", "20")
    assert lim.help == "Requests per minute; raise on big hosts"

    repo = tmp_path
    (repo / "env.example").write_text(new_t)
    (repo / ".env").write_text(current)
    mig.apply_migration(repo, plan, {"FEATURE_X": "true"})
    text = (repo / ".env").read_text()
    vals = load(repo / ".env")
    # operator values carried, new key set where the template documents it, removed key kept under the marker
    assert vals == {"API_URL": "http://mine", "KEEP": "1", "LIMIT": "10", "FEATURE_X": "true", "OPT": "set", "GONE": "bye", "MINE": "hand-added"}
    assert "# Brand new switch\n# (two lines of help)\nFEATURE_X=true\n" in text
    assert "# Requests per minute" in text
    assert text.index("GONE=bye") > text.index("set by orizonctl")
    assert "\nOPT=set\n" in text and "# OPT=" not in text


def test_migration_moves_a_key_to_its_new_file(tmp_path):
    repo = tmp_path
    (repo / "env.example").write_text("APP=1\nVLLM_PORT=8001\n")
    (repo / "env.example.vllm").write_text("DEFAULT_MODEL=m\n")
    (repo / ".env").write_text("APP=1\n")
    (repo / ".env.vllm").write_text("DEFAULT_MODEL=m\nVLLM_PORT=9001\n")
    old = {EnvFile.ENV: "APP=1\n", EnvFile.VLLM: "DEFAULT_MODEL=m\nVLLM_PORT=8001\n"}
    plans = {p.file: p for p in mig.plan_migration(repo, old)}
    new = next(c for c in plans[EnvFile.ENV].changes if c.key == "VLLM_PORT")
    gone = next(c for c in plans[EnvFile.VLLM].changes if c.key == "VLLM_PORT")
    assert (new.kind, new.current, new.moved) == (mig.NEW, "9001", EnvFile.VLLM)
    assert (gone.kind, gone.moved) == (mig.MOVED, EnvFile.ENV)
    mig.apply_migration(repo, plans[EnvFile.ENV], {"VLLM_PORT": new.current})
    mig.apply_migration(repo, plans[EnvFile.VLLM], {})
    assert load(repo / ".env") == {"APP": "1", "VLLM_PORT": "9001"}
    assert load(repo / ".env.vllm") == {"DEFAULT_MODEL": "m"}


def test_migration_without_old_templates_only_sees_new_and_removed():
    plan = mig.plan_file(EnvFile.ENV, "A=1\nB=2\n", "A=x\nC=3\n", None)
    assert {c.key: c.kind for c in plan.changes} == {"B": mig.NEW, "C": mig.EXTRA}


# ---------------------------------------------------------------- update


def test_latest_release_prefers_plain_tags():
    assert latest_release(["v1.1.0", "v1.3.0_dev", "v1.2.0", "latest"]) == "v1.2.0"
    assert latest_release(["v1.3.0_dev", "v1.2.9_rc1"]) == "v1.3.0_dev"
    assert latest_release([]) is None


def _grow_template(repo: Path):
    """Simulate a new release whose env.example adds a key and moves a default."""
    t = (repo / "env.example").read_text()
    t = t.replace("SERVER_PORT=8005", "SERVER_PORT=8006", 1)
    t += "\n# Sample rate for the new tracing feature (0..1)\nTRACE_SAMPLE_RATE=0.1\n"
    (repo / "env.example").write_text(t)


def new_release_arrives(monkeypatch):
    """The new release's deployment files land when `update` materializes the
    bundle; here that step grows the template instead."""
    monkeypatch.setattr(cli, "is_bundle_available", lambda: True)
    monkeypatch.setattr(cli, "_materialize", lambda ctx, force: _grow_template(ctx.app_dir))


def test_update_latest_snapshots_migrates_and_bumps(deployed, monkeypatch):
    d = deployed
    repo = d["repo"]
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: ["v1.1.0", "v1.2.0", "v1.3.0", "v1.4.0_dev"])
    new_release_arrives(monkeypatch)
    assert "TRACE_SAMPLE_RATE" not in load(repo / ".env")

    r = invoke(d, "update", "--latest", "--no-cli-upgrade", "--non-interactive", "--set", "TRACE_SAMPLE_RATE=0.5", "--yes")
    assert r.exit_code == 0, r.output
    assert "Update v1.2.0 -> v1.3.0" in r.output
    env = load(repo / ".env")
    assert env["ORIZON_VERSION"] == "v1.3.0"
    assert env["TRACE_SAMPLE_RATE"] == "0.5"  # new key from --set
    assert env["SERVER_PORT"] == "8005"  # changed default: the operator's value is kept unattended
    assert State.load(repo).version == "v1.3.0"
    # the images were pulled at the target tag before anything changed
    stack = d["stacks"][-1]
    pulls = [c for c in stack.calls if c[0] == "run" and c[1][0] == "pull"]
    assert pulls and pulls[0][3] == {"ORIZON_VERSION": "v1.3.0"}
    # the snapshot holds the pre-update files and is marked applied
    snaps = bk.list_snapshots(d["home"])
    assert len(snaps) == 1 and snaps[0].manifest.kind == "update"
    assert snaps[0].manifest.to_version == "v1.3.0" and snaps[0].manifest.status == "applied"
    assert load(snaps[0].dir / bk.ENV_DIR / ".env")["ORIZON_VERSION"] == "v1.2.0"
    assert "orizonctl rollback" in r.output


def test_update_interactive_asks_for_new_keys_and_offers_keep(deployed, monkeypatch):
    d = deployed
    repo = d["repo"]
    new_release_arrives(monkeypatch)
    monkeypatch.setattr(cli, "_on_terminal", lambda: True)  # CliRunner's stdin is not a tty
    # answers: continue? [y]; TRACE_SAMPLE_RATE -> 0.25; SERVER_PORT: [n]ew default; another key: FRONTEND_PORT=3100; then Enter
    answers = "y\n0.25\nn\nFRONTEND_PORT=3100\n\n"
    r = invoke(d, "update", "--version", "v1.3.0", "--no-cli-upgrade", input=answers)
    assert r.exit_code == 0, r.output
    assert "Sample rate for the new tracing feature" in r.output  # the template's help shown
    env = load(repo / ".env")
    assert env["TRACE_SAMPLE_RATE"] == "0.25"
    assert env["SERVER_PORT"] == "8006"  # took the new default
    assert env["FRONTEND_PORT"] == "3100"  # an existing key changed on request
    assert env["ORIZON_VERSION"] == "v1.3.0"
    # a later update with the same templates has nothing to migrate
    monkeypatch.setattr(cli, "_materialize", lambda ctx, force: None)
    r = invoke(d, "update", "--no-cli-upgrade", "--non-interactive", "--yes", "--no-backup")
    assert r.exit_code == 0, r.output
    assert "nothing to migrate" in r.output


def test_update_latest_when_already_newest(deployed, monkeypatch):
    d = deployed
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: ["v1.1.0", "v1.2.0"])
    monkeypatch.setattr(cli, "is_bundle_available", lambda: False)
    r = invoke(d, "update", "--latest", "--no-cli-upgrade", "--non-interactive", "--yes", "--no-backup")
    assert r.exit_code == 0, r.output
    assert "already the newest" in r.output
    assert load(d["repo"] / ".env")["ORIZON_VERSION"] == "v1.2.0"


def test_update_latest_needs_tag_listing(deployed, monkeypatch):
    d = deployed
    monkeypatch.setattr(registry, "list_tags", lambda prefix, image, creds=None, timeout=20: [])
    r = CliRunner().invoke(main, ["--home", str(d["home"]), "--app-dir", str(d["repo"]), "update", "--latest", "--non-interactive"])
    assert r.exit_code != 0 and "pass --version" in r.output


def test_update_failure_points_at_the_snapshot(deployed, monkeypatch):
    d = deployed
    monkeypatch.setattr(cli, "is_bundle_available", lambda: False)
    monkeypatch.setattr(cli, "_up", lambda *a, **k: (_ for _ in ()).throw(cli.ComposeError("boom")))
    r = CliRunner().invoke(main, ["--home", str(d["home"]), "--app-dir", str(d["repo"]), "update", "--version", "v1.3.0", "--no-cli-upgrade", "--non-interactive", "--yes"])
    assert r.exit_code == 1
    snap = bk.list_snapshots(d["home"])[-1]
    assert f"orizonctl rollback {snap.name}" in r.output


# -------------------------------------------------------------- rollback


def test_rollback_restores_files_database_and_data(deployed, monkeypatch):
    d = deployed
    repo = d["repo"]
    new_release_arrives(monkeypatch)
    r = invoke(d, "update", "--version", "v1.3.0", "--no-cli-upgrade", "--non-interactive", "--yes")
    assert r.exit_code == 0, r.output
    assert load(repo / ".env")["ORIZON_VERSION"] == "v1.3.0"
    # the client changed data after the update; the snapshot's file was deleted, a new one created
    (repo / "backend-orizon-ai" / "data" / "upload.bin").unlink()
    (repo / "backend-orizon-ai" / "data" / "newer.bin").write_bytes(b"after")
    (repo / "docker-compose.yml").write_text("# replaced by the new release\n")

    r = invoke(d, "rollback", "--yes")
    assert r.exit_code == 0, r.output
    assert load(repo / ".env")["ORIZON_VERSION"] == "v1.2.0"
    assert "TRACE_SAMPLE_RATE" not in load(repo / ".env")
    assert State.load(repo).version == "v1.2.0"
    assert "services:" in (repo / "docker-compose.yml").read_text()  # previous release's compose file is back
    assert (repo / "backend-orizon-ai" / "data" / "upload.bin").read_bytes() == b"client bytes"
    assert (repo / "backend-orizon-ai" / "data" / "newer.bin").exists()  # never deleted
    stack = d["stacks"][-1]
    downs = [c for c in stack.calls if c[0] == "run" and c[1][0] == "down"]
    assert downs, "the stack was stopped before files were restored"
    assert stack.restored == DUMP
    execs = [c for c in stack.calls if c[0] == "exec"]
    assert any("pg_terminate_backend" in " ".join(c[2]) for c in execs)
    assert any(c[2][0] == "pg_restore" and "--clean" in c[2] for c in execs)
    snap = bk.list_snapshots(d["home"])[-1]
    assert snap.manifest.status == "rolled-back"


def test_rollback_db_only_and_no_db(deployed):
    d = deployed
    r = invoke(d, "backup", "--no-data")
    assert r.exit_code == 0, r.output
    name = bk.list_snapshots(d["home"])[-1].name
    r = invoke(d, "rollback", name, "--db-only", "--yes")
    assert r.exit_code == 0, r.output
    stack = d["stacks"][-1]
    assert stack.restored == DUMP
    assert not any(c[0] == "run" and c[1][0] == "down" for c in stack.calls)

    r = invoke(d, "rollback", name, "--no-db", "--yes")
    assert r.exit_code == 0, r.output
    stack = d["stacks"][-1]
    assert stack.restored is None

    r = CliRunner().invoke(main, ["--home", str(d["home"]), "--app-dir", str(d["repo"]), "rollback", "nope", "--yes"])
    assert r.exit_code != 0 and "no snapshot found" in r.output


def test_update_failure_on_a_terminal_can_roll_back_at_once(deployed, monkeypatch):
    d = deployed
    new_release_arrives(monkeypatch)
    monkeypatch.setattr(cli, "_on_terminal", lambda: True)
    calls = {"up": 0}

    def flaky_up(ctx, pull, build, skip_preflight, download):
        calls["up"] += 1
        if calls["up"] == 1:
            raise cli.ComposeError("frontend unhealthy")
        return True

    monkeypatch.setattr(cli, "_up", flaky_up)
    # continue? y; TRACE_SAMPLE_RATE: default; SERVER_PORT: keep; another key: none; roll back now? y
    r = CliRunner().invoke(main, ["--home", str(d["home"]), "--app-dir", str(d["repo"]), "update", "--version", "v1.3.0", "--no-cli-upgrade"], input="y\n\n\n\ny\n")
    assert r.exit_code == 1, r.output
    assert "Rollback to" in r.output
    assert load(d["repo"] / ".env")["ORIZON_VERSION"] == "v1.2.0"
    assert "TRACE_SAMPLE_RATE" not in load(d["repo"] / ".env")
    assert bk.list_snapshots(d["home"])[-1].manifest.status == "rolled-back"


class ProfileStack(FakeStack):
    """`mcp-okasa` has a container but its profile is off; `mcp-hubspot` is active."""

    def run(self, *args, mcp=False, check=True, capture=False, timeout=None, env=None, pins=True):
        import subprocess

        self.calls.append(("run", args, mcp, env))
        if args[:3] == ("--profile", "*", "config"):
            out = "mcp-hubspot\nmcp-okasa\n"
        elif args[:2] == ("config", "--services"):
            out = "mcp-hubspot\n"
        else:
            out = ""
        return subprocess.CompletedProcess(args, 0, out, "")

    def container_ids(self, service, mcp=False):
        return ["c1"] if service in ("mcp-hubspot", "mcp-okasa") else []


def test_remove_inactive_removes_only_containers_whose_profile_is_off(tmp_path):
    stack = ProfileStack(tmp_path)
    assert stack.remove_inactive(mcp=True) == ["mcp-okasa"]
    assert [c[1] for c in stack.calls if c[1][0] == "rm"] == [("rm", "-sf", "mcp-okasa")]


def test_remove_inactive_does_nothing_when_every_profile_is_on(tmp_path):
    stack = FakeStack(tmp_path)
    assert stack.remove_inactive() == []
    assert not any(c[1][0] == "rm" for c in stack.calls if c[0] == "run")


# ------------------------------------------------------- one service alone

PIN_FILE = Path(".orizon") / "overrides.yml"
MCP_PIN_FILE = Path(".orizon") / "overrides.mcp.yml"


def test_update_one_service_pins_its_image_and_recreates_only_it(deployed):
    d = deployed
    repo = d["repo"]
    r = invoke(d, "update", "backend", "--version", "v1.2.1-hotfix", "--yes")
    assert r.exit_code == 0, r.output
    ref = "example.com/orizon/orizon-ai-backend:v1.2.1-hotfix"
    st = State.load(repo)
    assert st.image_pins == {"backend": ref} and st.version == "v1.2.0"
    assert load(repo / ".env")["ORIZON_VERSION"] == "v1.2.0"  # the release did not move
    assert f'image: "{ref}"' in (repo / PIN_FILE).read_text()
    stack = d["stacks"][-1]
    assert ("pull_image", ref) in stack.calls
    ups = [c[1] for c in stack.calls if c[0] == "run" and c[1][0] == "up"]
    assert ups == [("up", "-d", "--no-deps", "--no-build", "backend")]
    snap = bk.list_snapshots(d["home"])[-1]
    assert snap.manifest.to_version == "backend:v1.2.1-hotfix" and snap.manifest.status == "applied"
    # every later compose call reads the pin file after the main one
    base = Stack(repo)._base(False)
    assert base[base.index(str(repo / "docker-compose.yml")) + 2] == str(repo / PIN_FILE)
    assert str(repo / PIN_FILE) not in Stack(repo)._base(False, pins=False) and str(repo / PIN_FILE) not in Stack(repo)._base(True)
    r = invoke(d, "info")
    assert "pinned: backend" in r.output and ref in r.output


def test_update_one_service_with_a_full_image_then_unpin(deployed):
    d = deployed
    repo = d["repo"]
    r = invoke(d, "update", "backend", "--image", "other.registry/team/backend:test-7", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    assert State.load(repo).image_pins == {"backend": "other.registry/team/backend:test-7"}
    assert bk.list_snapshots(d["home"]) == []

    # re-pull of the pinned image keeps the pin
    r = invoke(d, "update", "backend", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    assert ("pull_image", "other.registry/team/backend:test-7") in d["stacks"][-1].calls
    assert State.load(repo).image_pins == {"backend": "other.registry/team/backend:test-7"}

    # configure does not forget it
    cfg = repo / "a.json"
    cfg.write_text(json.dumps(ANSWERS))
    assert invoke(d, "configure", "--non-interactive", "--config", str(cfg)).exit_code == 0
    assert State.load(repo).image_pins == {"backend": "other.registry/team/backend:test-7"}

    r = invoke(d, "update", "backend", "--unpin", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    assert State.load(repo).image_pins == {} and not (repo / PIN_FILE).exists()
    assert ("pull_image", "example.com/orizon/orizon-ai-backend:v1.2.0") in d["stacks"][-1].calls


def test_update_one_service_to_the_release_tag_is_not_a_pin(deployed):
    d = deployed
    r = invoke(d, "update", "backend", "frontend", "--version", "v1.2.0", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    assert State.load(d["repo"]).image_pins == {}
    ups = [c[1] for c in d["stacks"][-1].calls if c[0] == "run" and c[1][0] == "up"]
    assert ups == [("up", "-d", "--no-deps", "--no-build", "backend", "frontend")]


def test_update_one_service_refuses_what_it_cannot_do(deployed):
    d = deployed
    r = invoke(d, "update", "nope", "--version", "v9", "--yes")
    assert r.exit_code != 0 and "not a service of the configured stack: nope" in r.output
    r = invoke(d, "update", "db", "--version", "v9", "--yes")
    assert r.exit_code != 0 and "does not follow ORIZON_VERSION" in r.output
    r = invoke(d, "update", "backend", "frontend", "--image", "a/b:c", "--yes")
    assert r.exit_code != 0 and "exactly one service" in r.output
    r = invoke(d, "update", "backend", "--image", "a/b:c", "--unpin", "--yes")
    assert r.exit_code != 0 and "exclusive" in r.output
    r = invoke(d, "update", "backend", "--latest", "--yes")
    assert r.exit_code != 0 and "release update" in r.output
    r = invoke(d, "update", "--unpin", "--yes")
    assert r.exit_code != 0 and "need a service name" in r.output
    assert State.load(d["repo"]).image_pins == {} and bk.list_snapshots(d["home"]) == []


def test_update_one_service_unreachable_image_changes_nothing(deployed, monkeypatch):
    d = deployed
    monkeypatch.setattr(cli, "image_accessible", lambda ref: (False, "manifest unknown"))
    monkeypatch.setattr(FakeStack, "docker", lambda self, *a, **k: __import__("subprocess").CompletedProcess(a, 1, "", ""))
    r = invoke(d, "update", "backend", "--version", "v9.9.9", "--yes")
    assert r.exit_code == 1 and "cannot reach example.com/orizon/orizon-ai-backend:v9.9.9" in r.output
    assert State.load(d["repo"]).image_pins == {} and not (d["repo"] / PIN_FILE).exists()
    assert not [c for c in d["stacks"][-1].calls if c[0] == "run" and c[1][0] == "up"]
    assert bk.list_snapshots(d["home"])[-1].manifest.status == "update-failed"


def test_release_update_drops_pins_unless_asked_to_keep_them(deployed, monkeypatch):
    d = deployed
    repo = d["repo"]
    assert invoke(d, "update", "backend", "--version", "v1.2.1-hotfix", "--no-backup", "--yes").exit_code == 0
    r = invoke(d, "update", "--version", "v1.3.0", "--no-cli-upgrade", "--non-interactive", "--no-backup", "--keep-pins", "--yes")
    assert r.exit_code == 0, r.output
    assert State.load(repo).image_pins and (repo / PIN_FILE).exists()
    r = invoke(d, "update", "--version", "v1.4.0", "--no-cli-upgrade", "--non-interactive", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    assert "image pins dropped" in r.output
    assert State.load(repo).image_pins == {} and not (repo / PIN_FILE).exists()


def test_rollback_puts_the_pins_of_the_snapshot_back(deployed):
    d = deployed
    repo = d["repo"]
    r = invoke(d, "update", "backend", "--version", "v1.2.1-hotfix", "--yes")  # snapshot: nothing pinned
    assert r.exit_code == 0 and (repo / PIN_FILE).exists()
    r = invoke(d, "rollback", "--yes", "--no-cli")
    assert r.exit_code == 0, r.output
    assert State.load(repo).image_pins == {} and not (repo / PIN_FILE).exists()


# ------------------------------------------------------ an added container


def with_mcp(repo):
    st = State.load(repo)
    st.mcp = True
    st.save(repo)


def test_service_add_a_connector_wires_it_and_remove_takes_it_back(deployed):
    d = deployed
    repo = d["repo"]
    with_mcp(repo)
    hosts_before = load(repo / ".env.mcp")["MCP_ALLOWED_HOSTS"]
    assert "MCP_NEWONE_URL" not in load(repo / ".env") and "mcp-newone:8150" not in hosts_before

    r = invoke(d, "service", "add", "mcp-newone", "--port", "8150", "--yes")
    assert r.exit_code == 0, r.output
    image = "example.com/orizon/orizon-mcp-newone:v1.2.0"  # <prefix>/orizon-<name>:<ORIZON_VERSION>
    spec = State.load(repo).extra_services["mcp-newone"]
    assert spec["image"] == image and spec["mcp"] is True and spec["port"] == 8150
    text = (repo / MCP_PIN_FILE).read_text()
    assert f'image: "{image}"' in text and "container_name: \"orizon-mcp-newone\"" in text
    assert "- .env.mcp" in text and 'MCP_PORT: "8150"' in text and 'MCP_CRED_VARS: ""' in text and "healthcheck:" in text
    assert "ports:" not in text  # not published on the host unless asked
    assert not (repo / PIN_FILE).exists()
    assert load(repo / ".env")["MCP_NEWONE_URL"] == "http://mcp-newone:8150/mcp"
    assert load(repo / ".env.mcp")["MCP_ALLOWED_HOSTS"] == hosts_before + ",mcp-newone:8150,localhost:8150"
    stack = d["stacks"][-1]
    assert ("pull_image", image) in stack.calls
    ups = [(c[1], c[2]) for c in stack.calls if c[0] == "run" and c[1][0] == "up"]
    assert ups == [(("up", "-d", "--no-deps", "--no-build", "mcp-newone"), True)]
    assert "added: mcp-newone" in invoke(d, "info").output and "mcp-newone" in invoke(d, "service", "list").output

    # its image moves like any other service's, and configure does not forget it
    r = invoke(d, "update", "mcp-newone", "--version", "v1.4.0_dev", "--no-backup", "--yes")
    assert r.exit_code != 0  # FakeStack lists no MCP services, so the name is unknown there
    cfg = repo / "a.json"
    cfg.write_text(json.dumps({**ANSWERS, "mcp": True}))
    assert invoke(d, "configure", "--non-interactive", "--config", str(cfg)).exit_code == 0
    assert "mcp-newone" in State.load(repo).extra_services

    r = invoke(d, "service", "remove", "mcp-newone", "--yes")
    assert r.exit_code == 0, r.output
    assert State.load(repo).extra_services == {} and not (repo / MCP_PIN_FILE).exists()
    assert "MCP_NEWONE_URL" not in load(repo / ".env")
    assert load(repo / ".env.mcp")["MCP_ALLOWED_HOSTS"] == hosts_before
    rms = [c[1] for c in d["stacks"][-1].calls if c[0] == "run" and c[1][0] == "rm"]
    assert rms == [("rm", "-sf", "mcp-newone")]


def test_service_add_a_plain_container_on_the_main_stack(deployed):
    d = deployed
    repo = d["repo"]
    r = invoke(d, "service", "add", "my-tool", "--image", "reg.example/team/tool:1", "--port", "9000", "--publish", "--env", "MODE=prod", "--env", "PRICE=$5", "--yes")
    assert r.exit_code == 0, r.output
    text = (repo / PIN_FILE).read_text()
    assert 'image: "reg.example/team/tool:1"' in text and '- "9000:9000"' in text and 'MODE: "prod"' in text
    assert 'PRICE: "$$5"' in text  # literal for Compose
    assert "env_file" not in text and "MCP_PORT" not in text
    st = State.load(repo)
    assert "my-tool" in st.app_services() and "my-tool" in st.all_services()  # `up` starts it with the application
    assert st.required_ports({})[9000] == "my-tool"
    assert load(repo / ".env.mcp")["MCP_ALLOWED_HOSTS"].count("my-tool") == 0

    # another image for it: the recorded one is replaced, no pin
    r = invoke(d, "update", "my-tool", "--version", "2", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    st = State.load(repo)
    assert st.extra_services["my-tool"]["image"] == "reg.example/team/tool:2" and st.image_pins == {}
    r = invoke(d, "update", "my-tool", "--unpin", "--no-backup", "--yes")
    assert r.exit_code != 0 and "service remove my-tool" in r.output


def test_service_add_refuses_what_it_cannot_do(deployed, monkeypatch):
    d = deployed
    r = invoke(d, "service", "add", "mcp-newone", "--port", "8150", "--yes")
    assert r.exit_code != 0 and "stack is off" in r.output  # connectors not enabled here
    r = invoke(d, "service", "add", "tool", "--yes")
    assert r.exit_code != 0 and "--image is needed" in r.output
    r = invoke(d, "service", "add", "Bad Name", "--image", "a/b:c", "--yes")
    assert r.exit_code != 0 and "not a service name" in r.output
    monkeypatch.setattr(FakeStack, "every_service", lambda self, mcp=False, pins=True: ["db", "backend", "frontend"])
    r = invoke(d, "service", "add", "backend", "--image", "a/b:c", "--yes")
    assert r.exit_code != 0 and "already a service of docker-compose.yml" in r.output
    r = invoke(d, "service", "remove", "backend", "--yes")
    assert r.exit_code != 0 and "was not added" in r.output
    assert State.load(d["repo"]).extra_services == {}


def test_release_update_drops_an_added_service_the_release_now_defines(deployed, monkeypatch):
    d = deployed
    repo = d["repo"]
    assert invoke(d, "service", "add", "my-tool", "--image", "reg.example/team/tool:1", "--yes").exit_code == 0
    monkeypatch.setattr(FakeStack, "every_service", lambda self, mcp=False, pins=True: ["db", "backend", "frontend", "my-tool"])
    r = invoke(d, "update", "--version", "v1.3.0", "--no-cli-upgrade", "--non-interactive", "--no-backup", "--yes")
    assert r.exit_code == 0, r.output
    assert "defined by this release's compose file now" in r.output
    assert State.load(repo).extra_services == {} and not (repo / PIN_FILE).exists()


# ------------------------------------------------------------- self-update


def test_self_update_reinstalls_the_cli_at_the_running_release(deployed, monkeypatch):
    d = deployed
    ran: list[list[str]] = []
    import subprocess

    def fake_run(cmd, **kw):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "orizonctl, version 9.9.9\n", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, **kw: ran.append(list(cmd)) or 0)
    env_before = (d["repo"] / ".env").read_text()
    r = invoke(d, "self-update")
    assert r.exit_code == 0, r.output
    pips = [c for c in ran if "pip" in c]
    assert [c[-1] for c in pips] == ["/nonexistent/v1.2.0"] * 2  # the template at ORIZON_VERSION
    assert "--upgrade" in pips[0] and "--force-reinstall" in pips[1] and "--no-deps" in pips[1]
    assert ran[-1][-1] == "init"  # the new copy writes its bundled files
    assert (d["repo"] / ".env").read_text() == env_before

    ran.clear()
    r = invoke(d, "self-update", "--ref", "main", "--no-files")
    assert r.exit_code == 0, r.output
    assert [c[-1] for c in ran if "pip" in c] == ["/nonexistent/main"] * 2 and ran[-1][-1] == "--version"
