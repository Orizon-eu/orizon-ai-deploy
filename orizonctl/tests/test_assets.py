import subprocess
from pathlib import Path

from orizonctl import assets

HERE = Path(__file__).resolve().parent.parent


def test_bundle_matches_monorepo():
    """A compose or template change that was not synced must fail CI."""
    r = subprocess.run(["bash", str(HERE / "sync-assets.sh"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_bundle_lists_the_essentials():
    files = assets.bundled_files()
    for must in ("docker-compose.yml", "docker-compose.mcp.yml", "env.example", "env.example.secrets", "nginx/orizonai.conf.template", "setup-ssl.sh", "setup-sandbox-host.sh", "apparmor/rootlesskit"):
        assert must in files
    assert not any(f.startswith(("backend-orizon-ai", "frontend-orizon-ai", "mcp-servers/server")) for f in files)


def test_materialize_writes_then_respects_local_edits(tmp_path):
    rep = assets.materialize(tmp_path)
    assert "docker-compose.yml" in rep.written and not rep.kept_local
    assert (tmp_path / "setup-ssl.sh").stat().st_mode & 0o111  # executable
    assert assets.app_dir_is_materialized(tmp_path)

    # second run: nothing to do
    rep2 = assets.materialize(tmp_path)
    assert not rep2.written and "docker-compose.yml" in rep2.unchanged

    # operator edits a file -> kept, unless forced (then backed up)
    target = tmp_path / "docker-compose.yml"
    target.write_text(target.read_text() + "# local tweak\n")
    rep3 = assets.materialize(tmp_path)
    assert "docker-compose.yml" in rep3.kept_local and "# local tweak" in target.read_text()
    rep4 = assets.materialize(tmp_path, force=True)
    assert "docker-compose.yml" in rep4.overwritten_local and "# local tweak" not in target.read_text()
    assert rep4.backup_dir and (rep4.backup_dir / "docker-compose.yml").read_text().endswith("# local tweak\n")

    # .env files are never part of the bundle
    (tmp_path / ".env").write_text("X=1\n")
    assets.materialize(tmp_path, force=True)
    assert (tmp_path / ".env").read_text() == "X=1\n"


def test_materialize_over_a_dev_checkout_keeps_unrecorded_files(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    rep = assets.materialize(tmp_path)
    assert "docker-compose.yml" in rep.kept_local
    assert (tmp_path / "docker-compose.yml").read_text() == "services: {}\n"
