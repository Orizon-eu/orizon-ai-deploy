from orizonctl import assets, doctor
from orizonctl.compose import Stack
from orizonctl.preflight import backend_cpu_check
from orizonctl.state import State


def test_cpu_check_fails_when_the_default_limit_exceeds_the_host():
    c = backend_cpu_check({}, cpus=8)
    assert c.status == "fail"
    assert "BACKEND_CPU_LIMIT=8.0" in c.hint


def test_cpu_check_warns_below_16_when_the_limit_fits():
    c = backend_cpu_check({"BACKEND_CPU_LIMIT": "8.0"}, cpus=8)
    assert c.status == "warn" and "16" in c.detail


def test_cpu_check_ok_on_16_or_more():
    assert backend_cpu_check({}, cpus=16).status == "ok"
    assert backend_cpu_check({}, cpus=32).status == "ok"


def test_cpu_check_rejects_a_non_number():
    assert backend_cpu_check({"BACKEND_CPU_LIMIT": "lots"}, cpus=32).status == "fail"


def test_drifted_lists_files_that_differ_from_the_bundle(tmp_path):
    assets.materialize(tmp_path)
    assert assets.drifted(tmp_path) == []
    target = tmp_path / "docker-compose.yml"
    target.write_text(target.read_text() + "\n# older copy\n")
    assert assets.drifted(tmp_path) == ["docker-compose.yml"]


def _fake_stack(monkeypatch, tmp_path, statuses, defined, active, backend_log=""):
    monkeypatch.setattr(Stack, "service_status", lambda self, s, mcp=False: statuses.get(s, "ready"))
    monkeypatch.setattr(Stack, "config_services", lambda self, mcp=False: active)
    monkeypatch.setattr(Stack, "every_service", lambda self, mcp=False: defined)
    monkeypatch.setattr(Stack, "container_ids", lambda self, s, mcp=False: [] if statuses.get(s) == "absent" else ["id"])
    monkeypatch.setattr(Stack, "inspect_state", lambda self, cid: ("running", "healthy", 0))
    monkeypatch.setattr(Stack, "tail", lambda self, s, lines=60, mcp=False: backend_log)
    monkeypatch.setattr(doctor, "http_ok", lambda url, timeout=5: "8005" not in url or not backend_log)
    monkeypatch.setattr(doctor.h, "docker_root_dir", lambda: None)
    monkeypatch.setattr(doctor.h, "cpu_count", lambda: 16)
    return Stack(tmp_path)


def test_doctor_names_an_undefined_service_and_the_real_fix(monkeypatch, tmp_path):
    st = State(text_mode="external", embedding_mode="external", rerank_mode="off", transcription=True)
    defined = ["db", "frontend", "backend", "transcription-vllm-server", "transcription-bot", "nginx"]
    stack = _fake_stack(monkeypatch, tmp_path, {"diarization-server": "absent", "transcription-bot": "absent"}, defined, defined)
    checks = {c.name: c for c in doctor.report(stack, st, {})}
    diar = checks["container diarization-server"]
    assert diar.status == "fail" and diar.detail == "not defined in docker-compose.yml"
    assert "orizonctl init --force" in diar.hint
    bot = checks["container transcription-bot"]
    assert bot.detail == "not created" and bot.hint == "orizonctl up"


def test_doctor_reports_a_rejected_database_password(monkeypatch, tmp_path):
    st = State(text_mode="external", embedding_mode="external", rerank_mode="off")
    log = '{"is_panic":false,"message":"Authentication failed","error_code":"P1000"}'
    stack = _fake_stack(monkeypatch, tmp_path, {"backend": "failed:unhealthy"}, [], [], backend_log=log)
    checks = {c.name: c for c in doctor.report(stack, st, {})}
    assert checks["database login"].status == "fail"
    assert "orizonctl up" in checks["database login"].hint
