import json

from click.testing import CliRunner

from orizonctl import hostinfo
from orizonctl.cli import main


def test_configure_non_interactive_writes_files(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(hostinfo, "nvidia_gpus", lambda: [])
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({
        "version": "v1.2.0",
        "models": {"text": "external", "embedding": "external", "rerank": "off"},
        "external": {"text": {"url": "http://llm.internal:8000", "model": "qwen"}, "embedding": {"url": "http://emb.internal:8000", "model": "nomic-ai/nomic-embed-text-v2-moe"}},
        "access": {"mode": "external-proxy", "public_url": "https://ai.corp.example"},
    }))
    r = CliRunner().invoke(main, ["--home", str(tmp_path / "home"), "--app-dir", str(repo), "configure", "--non-interactive", "--answers", str(answers)])
    assert r.exit_code == 0, r.output
    assert (repo / ".env").exists() and (repo / ".env.secrets").exists()
    assert "wrote" in r.output
    # secrets are masked in the summary
    from orizonctl.envfile import load

    secrets = load(repo / ".env.secrets")
    assert secrets["ORIZON_MCP_ENCRYPTION_KEY"] not in r.output
    assert secrets["ORIZON_INTERNAL_API_KEY"] not in r.output
    assert "********" in r.output

    r2 = CliRunner().invoke(main, ["--home", str(tmp_path / "home"), "--app-dir", str(repo), "env", "show"])
    assert r2.exit_code == 0, r2.output
    assert "BETTER_AUTH_URL" in r2.output and "https://ai.corp.example" in r2.output

    r3 = CliRunner().invoke(main, ["--home", str(tmp_path / "home"), "--app-dir", str(repo), "info"])
    assert r3.exit_code == 0, r3.output
    assert "external-proxy" in r3.output


def test_missing_app_dir_is_a_clear_error(tmp_path):
    r = CliRunner().invoke(main, ["--home", str(tmp_path), "--app-dir", str(tmp_path / "nope"), "status"])
    assert r.exit_code != 0
    assert "orizonctl init" in r.output


def test_non_interactive_first_run_needs_answers(repo, tmp_path):
    r = CliRunner().invoke(main, ["--home", str(tmp_path), "--app-dir", str(repo), "configure", "--non-interactive"])
    assert r.exit_code != 0
    assert "--config" in r.output


def test_preflight_gate_asks_only_for_soft_failures(monkeypatch):
    import click
    import pytest

    from orizonctl.cli import _gate_preflight
    from orizonctl.preflight import Check

    ok = [Check("Memory", "ok", "64 GB")]
    _gate_preflight(ok, interactive=False)  # nothing to do

    soft = [Check("Memory", "fail", "12 GB"), Check("Port 3000", "fail", "in use")]
    with pytest.raises(click.ClickException, match="--skip-preflight"):
        _gate_preflight(soft, interactive=False)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
    _gate_preflight(soft, interactive=True)  # user said yes -> proceeds
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    with pytest.raises(click.Abort):
        _gate_preflight(soft, interactive=True)

    hard = [Check("Docker", "fail", "not installed")]
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
    with pytest.raises(click.ClickException, match="cannot be skipped"):
        _gate_preflight(hard, interactive=True)


def test_quick_preflight_only_hard_checks(repo):
    from orizonctl.preflight import HARD_CHECK_NAMES, run_checks

    checks = run_checks(repo, None, {}, quick=True)
    assert checks and all(c.name in HARD_CHECK_NAMES for c in checks)
    assert not any(c.name.startswith("Registry") or c.name == "Memory" for c in checks)
