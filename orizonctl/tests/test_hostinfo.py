"""GPU detection must agree with install.sh (`nvidia-smi -L`): a GPU whose
memory nvidia-smi reports as [N/A] (GB10 / DGX Spark, Jetson: unified memory)
is still a GPU."""

import subprocess

from orizonctl import hostinfo

QUERY = "--query-gpu=index,name,memory.total,driver_version"


def _fake_run(outputs):
    """outputs: {first distinctive arg: (returncode, stdout)}"""
    def run(cmd, timeout=30, **kw):
        for key, (rc, out) in outputs.items():
            if key in cmd:
                return subprocess.CompletedProcess(cmd, rc, out, "")
        return subprocess.CompletedProcess(cmd, 127, "", "not found")
    return run


def test_discrete_gpu_parses_memory(monkeypatch):
    monkeypatch.setattr(hostinfo, "run", _fake_run({QUERY: (0, "0, NVIDIA H100 80GB HBM3, 81559, 560.35\n")}))
    gpus = hostinfo.nvidia_gpus()
    assert len(gpus) == 1
    g = gpus[0]
    assert (g.index, g.name, g.memory_mib, g.driver, g.unified) == (0, "NVIDIA H100 80GB HBM3", 81559, "560.35", False)
    assert g.memory_label() == "79 GiB"


def test_unified_memory_gpu_is_not_dropped(monkeypatch):
    monkeypatch.setattr(hostinfo, "run", _fake_run({QUERY: (0, "0, NVIDIA GB10, [N/A], 580.173.02\n")}))
    monkeypatch.setattr(hostinfo, "_host_mem_mib", lambda: 128 * 1024)
    gpus = hostinfo.nvidia_gpus()
    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "NVIDIA GB10" and g.driver == "580.173.02" and g.unified
    assert g.memory_label() == "128 GiB unified"


def test_falls_back_to_nvidia_smi_L_when_the_query_fails(monkeypatch):
    monkeypatch.setattr(hostinfo, "run", _fake_run({
        QUERY: (1, ""),
        "-L": (0, "GPU 0: NVIDIA GB10 (UUID: GPU-1234)\nGPU 1: NVIDIA GB10 (UUID: GPU-5678)\n"),
        "--query-gpu=driver_version": (0, "580.173.02\n580.173.02\n"),
    }))
    gpus = hostinfo.nvidia_gpus()
    assert [(g.index, g.name, g.driver) for g in gpus] == [(0, "NVIDIA GB10", "580.173.02"), (1, "NVIDIA GB10", "580.173.02")]
    assert gpus[0].memory_label() == "memory n/a"


def test_no_nvidia_smi_means_no_gpus(monkeypatch):
    monkeypatch.setattr(hostinfo, "run", _fake_run({}))
    assert hostinfo.nvidia_gpus() == []
