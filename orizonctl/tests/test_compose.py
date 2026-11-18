import subprocess

from orizonctl.compose import Stack, db_credentials


def test_db_credentials_follow_the_url_the_backend_uses():
    values = {"DATABASE_URL": "postgresql://orizon:s%40cret@db:5432/orizon", "POSTGRES_USER": "postgres", "POSTGRES_PASSWORD": "other"}
    assert db_credentials(values) == ("orizon", "s@cret")


def test_db_credentials_fall_back_to_postgres_vars():
    assert db_credentials({"DATABASE_URL": "postgresql://u:p@elsewhere:5432/x", "POSTGRES_PASSWORD": "pw"}) == ("postgres", "pw")
    assert db_credentials({}) == ("postgres", "")


def test_sync_db_password_sends_the_password_on_stdin(tmp_path, monkeypatch):
    seen = {}

    def fake_exec(self, service, cmd, stdin=None, timeout=None, **kw):
        seen.update(service=service, cmd=cmd, stdin=stdin)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(Stack, "exec", fake_exec)
    ok = Stack(tmp_path).sync_db_password({"DATABASE_URL": "postgresql://postgres:it's@db:5432/orizon"})
    assert ok
    assert seen["service"] == "db"
    assert "it's" not in " ".join(seen["cmd"])
    assert seen["stdin"] == b"ALTER ROLE \"postgres\" WITH PASSWORD 'it''s';\n"


def test_sync_db_password_reports_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(Stack, "exec", lambda self, *a, **kw: subprocess.CompletedProcess([], 2, b"", b'FATAL:  role "postgres" does not exist'))
    assert not Stack(tmp_path).sync_db_password({"POSTGRES_PASSWORD": "pw"})


def test_sync_db_password_skips_without_a_password(tmp_path, monkeypatch):
    monkeypatch.setattr(Stack, "exec", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no exec expected")))
    assert Stack(tmp_path).sync_db_password({})


def test_explain_missing_tells_an_undefined_service_from_an_inactive_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Stack, "every_service", lambda self, mcp=False: ["db", "backend", "transcription-bot"])
    out = Stack(tmp_path).explain_missing(["transcription-bot", "diarization-server"])
    assert out["transcription-bot"][0] == "its compose profile is off"
    assert out["diarization-server"][0] == "not defined in docker-compose.yml"
    assert "orizonctl init --force" in out["diarization-server"][1]


def test_bring_up_fails_when_a_wanted_service_is_not_defined(tmp_path, monkeypatch):
    from orizonctl.state import State

    started = []
    monkeypatch.setattr(Stack, "config_services", lambda self, mcp=False: ["db", "frontend", "backend", "transcription-vllm-server", "transcription-bot", "nginx"])
    monkeypatch.setattr(Stack, "every_service", lambda self, mcp=False: ["db", "frontend", "backend", "transcription-vllm-server", "transcription-bot", "nginx"])
    monkeypatch.setattr(Stack, "pull", lambda self, services, mcp=False, env=None: None)
    monkeypatch.setattr(Stack, "up", lambda self, services, mcp=False, build=False, no_deps=True: started.extend(services))
    monkeypatch.setattr(Stack, "wait", lambda self, services, timeout, mcp=False, label="": True)
    monkeypatch.setattr(Stack, "sync_db_password", lambda self, values: True)
    monkeypatch.setattr(Stack, "_wait_http", lambda self, values, state, timeout=300: True)
    monkeypatch.setattr(Stack, "remove_inactive", lambda self, mcp=False: [])
    st = State(text_mode="external", embedding_mode="external", rerank_mode="off", transcription=True)
    assert Stack(tmp_path).bring_up(st, {}, defer_model_wait=True) is False
    assert "diarization-server" not in started and "backend" in started


def test_bring_up_sets_the_database_password_once_db_is_healthy(tmp_path, monkeypatch):
    from orizonctl.state import State

    order = []
    services = ["db", "frontend", "backend", "nginx"]
    monkeypatch.setattr(Stack, "config_services", lambda self, mcp=False: services)
    monkeypatch.setattr(Stack, "pull", lambda self, s, mcp=False, env=None: None)
    monkeypatch.setattr(Stack, "up", lambda self, s, mcp=False, build=False, no_deps=True: order.append(("up", tuple(s))))
    monkeypatch.setattr(Stack, "wait", lambda self, s, timeout, mcp=False, label="": order.append(("wait", tuple(s))) or True)
    monkeypatch.setattr(Stack, "sync_db_password", lambda self, values: order.append(("sync",)) or True)
    monkeypatch.setattr(Stack, "_wait_http", lambda self, values, state, timeout=300: True)
    monkeypatch.setattr(Stack, "remove_inactive", lambda self, mcp=False: [])
    st = State(text_mode="external", embedding_mode="external", rerank_mode="off")
    assert Stack(tmp_path).bring_up(st, {"POSTGRES_PASSWORD": "pw"}, defer_model_wait=True)
    assert order.index(("sync",)) == order.index(("wait", ("db",))) + 1
    assert order.index(("sync",)) < order.index(("up", ("frontend", "backend")))
