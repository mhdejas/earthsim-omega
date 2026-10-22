"""Hardware and software discovery plus adaptive resource profiles.

Every value reported here is measured on the running machine; nothing is
assumed.  Fields that cannot be detected are reported as ``None``.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

import psutil

GEO_LIBS = [
    "numpy", "scipy", "pandas", "rasterio", "geopandas", "shapely", "pyproj", "xarray",
    "rioxarray", "dask", "pyarrow", "sklearn", "torch", "pystac_client", "fastapi", "pydantic",
]


def _version(mod: str) -> str | None:
    try:
        from importlib.metadata import version

        dist = {"sklearn": "scikit-learn", "pystac_client": "pystac-client"}.get(mod, mod)
        return version(dist)
    except Exception:
        try:
            return getattr(importlib.import_module(mod), "__version__", "unknown")
        except Exception:
            return None


def _cpu_model() -> str | None:
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


ISA_OF_INTEREST = ["sse4_2", "avx", "avx2", "fma", "avx512f", "avx512dq", "avx512bw", "avx512vl", "avx512cd",
                   "avx512_vnni", "avx512_bf16", "amx_tile", "amx_bf16", "amx_int8", "f16c"]

# Intel family-6 model numbers -> microarchitecture (public Intel/Linux kernel tables).
_INTEL_MODELS = {85: "Skylake-SP / Cascade Lake / Cooper Lake (model 0x55)", 106: "Ice Lake-SP",
                 143: "Sapphire Rapids", 207: "Emerald Rapids", 79: "Broadwell-EP", 63: "Haswell-EP"}


def cpu_details() -> dict[str, Any]:
    """CPU identity as *reported by the (possibly virtualised) OS*.

    The model-name string inside a VM is set by the hypervisor and does not
    identify the host SKU; sustained clock is unknown. We therefore report the
    microarchitecture inferred from family/model/stepping + flags, not a SKU.
    """
    info: dict[str, Any] = {"vendor": None, "family": None, "model": None, "stepping": None, "flags": [],
                            "hypervisor": None, "l3_cache": None}
    try:
        txt = open("/proc/cpuinfo").read().split("\n\n")[0]
        for line in txt.splitlines():
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "vendor_id":
                info["vendor"] = v
            elif k == "cpu family":
                info["family"] = int(v)
            elif k == "model":
                info["model"] = int(v)
            elif k == "stepping":
                info["stepping"] = int(v)
            elif k == "flags":
                info["flags"] = v.split()
            elif k == "cache size":
                info["cache_size_reported"] = v
    except OSError:
        pass
    flags = set(info["flags"])
    info["virtualized"] = "hypervisor" in flags
    try:
        out = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            k, _, v = line.partition(":")
            if k.strip() == "Hypervisor vendor":
                info["hypervisor"] = v.strip()
            if k.strip() == "L3 cache":
                info["l3_cache"] = v.strip()
    except Exception:
        pass
    uarch = None
    if info["vendor"] == "GenuineIntel" and info["family"] == 6:
        uarch = _INTEL_MODELS.get(info["model"])
        if info["model"] == 85:
            st = info["stepping"] or 0
            if "avx512_bf16" in flags:
                uarch = "Cooper Lake (inferred: model 85 + AVX512_BF16)"
            elif "avx512_vnni" in flags and st >= 5:
                uarch = f"Cascade Lake (inferred: model 85, stepping {st}, AVX512_VNNI present)"
            else:
                uarch = f"Skylake-SP (inferred: model 85, stepping {st})"
    info["microarchitecture_inferred"] = uarch
    info["isa_supported"] = {f: (f in flags) for f in ISA_OF_INTEREST}
    info["sku_note"] = ("Virtualised CPU: the model-name string is hypervisor-provided; exact host SKU and "
                        "sustained clock are unknown." if info["virtualized"] else "bare metal (no hypervisor flag)")
    info.pop("flags")
    return info


def simd_runtime() -> dict[str, Any]:
    """What SIMD paths libraries *actually select at runtime* (not just CPU support).

    * numpy: ``numpy._core._multiarray_umath.__cpu_dispatch__`` lists dispatch
      targets compiled into this build; intersecting with ``__cpu_features__``
      gives the targets numpy's dispatcher will choose for kernels that have them.
    * torch: ``torch.backends.cpu.get_cpu_capability()`` reports the ATen
      vectorisation level selected at runtime (e.g. AVX512 / AVX2 / DEFAULT).
    VNNI is an int8 dot-product instruction: it is only *used* by int8-quantised
    kernels. EARTHSIM runs float32 workloads, so VNNI is recorded as not used.
    """
    out: dict[str, Any] = {}
    try:
        from numpy._core import _multiarray_umath as mu

        feats = mu.__cpu_features__
        baseline = list(getattr(mu, "__cpu_baseline__", []))
        dispatch = list(getattr(mu, "__cpu_dispatch__", []))
        out["numpy"] = {
            "baseline_compiled": baseline,
            "dispatch_targets_compiled": dispatch,
            "dispatch_targets_active": [d for d in dispatch if feats.get(d)],
            "note": "active = compiled dispatch target AND supported by this CPU; numpy selects these at runtime",
        }
    except Exception as e:
        out["numpy"] = {"error": repr(e)}
    try:
        import torch

        out["torch"] = {"aten_cpu_capability": torch.backends.cpu.get_cpu_capability(),
                        "mkldnn_available": torch.backends.mkldnn.is_available(),
                        "num_threads": torch.get_num_threads()}
    except Exception as e:
        out["torch"] = {"error": repr(e)}
    out["avx512_vnni_used"] = False
    out["avx512_vnni_note"] = "no int8-quantised kernels are executed by EARTHSIM workloads"
    return out


def _gpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {"available": False, "devices": [], "framework": None, "notes": []}
    try:
        import torch

        info["framework"] = f"torch {torch.__version__}"
        info["torch_cuda_build"] = torch.version.cuda
        if torch.cuda.is_available():
            info["available"] = True
            for i in range(torch.cuda.device_count()):
                p = torch.cuda.get_device_properties(i)
                info["devices"].append({"index": i, "name": p.name, "vram_bytes": p.total_memory})
        else:
            info["notes"].append("torch.cuda.is_available() is False")
    except Exception as e:  # torch missing
        info["notes"].append(f"torch not importable: {type(e).__name__}")
    smi = shutil.which("nvidia-smi")
    info["nvidia_smi"] = bool(smi)
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10)
            info["nvidia_smi_output"] = out.stdout.strip()
        except Exception as e:
            info["notes"].append(f"nvidia-smi failed: {e}")
    return info


def _internet(host: str = "earth-search.aws.element84.com", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class HardwareReport:
    os: str
    arch: str
    python: str
    executable: str
    cpu_model: str | None
    cpu: dict[str, Any]
    simd: dict[str, Any]
    logical_cores: int | None
    physical_cores: int | None
    ram_total_bytes: int
    ram_available_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    gpu: dict[str, Any]
    gdal_version: str | None
    gdal_cli: bool
    libraries: dict[str, str | None]
    internet: bool
    docker: bool
    docker_compose: bool
    recommended_profile: str = ""
    profile_settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect(check_internet: bool = True, path: str = ".") -> HardwareReport:
    vm = psutil.virtual_memory()
    du = shutil.disk_usage(path)
    try:
        import rasterio

        gdal = rasterio.__gdal_version__
    except Exception:
        gdal = None
    rep = HardwareReport(
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        python=sys.version.split()[0],
        executable=sys.executable,
        cpu_model=_cpu_model(),
        cpu=cpu_details(),
        simd=simd_runtime(),
        logical_cores=psutil.cpu_count(logical=True),
        physical_cores=psutil.cpu_count(logical=False),
        ram_total_bytes=vm.total,
        ram_available_bytes=vm.available,
        disk_total_bytes=du.total,
        disk_free_bytes=du.free,
        gpu=_gpu_info(),
        gdal_version=gdal,
        gdal_cli=bool(shutil.which("gdalinfo")),
        libraries={m: _version(m) for m in GEO_LIBS},
        internet=_internet() if check_internet else False,
        docker=bool(shutil.which("docker")),
        docker_compose=bool(shutil.which("docker-compose")) or bool(shutil.which("docker")),
    )
    prof = recommend_profile(rep)
    rep.recommended_profile = prof
    rep.profile_settings = profile_settings(prof, rep)
    return rep


def recommend_profile(rep: HardwareReport) -> str:
    gb = rep.ram_total_bytes / 1e9
    if os.environ.get("SLURM_JOB_ID"):
        return "hpc"
    if rep.gpu.get("available") and gb >= 32:
        return "workstation"
    if gb >= 64:
        return "high_memory"
    if (rep.logical_cores or 1) >= 8 and gb >= 16:
        return "workstation"
    return "development"


PROFILES: dict[str, dict[str, Any]] = {
    "development": {"worker_fraction": 0.5, "tile_size": 512, "max_memory_fraction": 0.35, "batch_size": 8},
    "workstation": {"worker_fraction": 0.75, "tile_size": 1024, "max_memory_fraction": 0.5, "batch_size": 32},
    "high_memory": {"worker_fraction": 0.75, "tile_size": 4096, "max_memory_fraction": 0.6, "batch_size": 64},
    "hpc": {"worker_fraction": 1.0, "tile_size": 2048, "max_memory_fraction": 0.7, "batch_size": 128},
}


def profile_settings(profile: str, rep: HardwareReport | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    rep = rep or detect(check_internet=False)
    p = dict(PROFILES[profile])
    cores = rep.logical_cores or 1
    p["workers"] = max(1, int(cores * p.pop("worker_fraction")))
    p["memory_budget_bytes"] = int(rep.ram_available_bytes * p["max_memory_fraction"])
    p["device"] = "cuda" if rep.gpu.get("available") else "cpu"
    if overrides:
        p.update({k: v for k, v in overrides.items() if v is not None})
    return p


def estimate_array_bytes(shape: tuple[int, ...], dtype: str = "float32") -> int:
    import numpy as np

    n = 1
    for s in shape:
        n *= int(s)
    return n * np.dtype(dtype).itemsize


def check_memory(shape: tuple[int, ...], dtype: str = "float32", copies: int = 1, budget: int | None = None) -> int:
    """Raise MemoryError *before* allocating if the estimate exceeds the budget."""
    need = estimate_array_bytes(shape, dtype) * copies
    budget = budget if budget is not None else int(psutil.virtual_memory().available * 0.5)
    if need > budget:
        raise MemoryError(
            f"Estimated {need/1e9:.2f} GB for array {shape} x{copies} ({dtype}) exceeds budget "
            f"{budget/1e9:.2f} GB. Use windowed processing or raise compute.max_memory_fraction."
        )
    return need


def summary_text(rep: HardwareReport) -> str:
    g = rep.gpu
    gpu = ", ".join(d["name"] for d in g["devices"]) if g.get("available") else "none detected (" + "; ".join(g.get("notes", [])) + ")"
    lines = [
        f"OS / arch        : {rep.os} / {rep.arch}",
        f"Python           : {rep.python} ({rep.executable})",
        f"CPU (reported)   : {rep.cpu_model}  vCPUs(logical)={rep.logical_cores} cores(reported)={rep.physical_cores}",
        f"CPU identity     : family {rep.cpu['family']} model {rep.cpu['model']} stepping {rep.cpu['stepping']}; "
        f"{rep.cpu['microarchitecture_inferred']}; hypervisor={rep.cpu['hypervisor']}; L3={rep.cpu['l3_cache']}",
        f"CPU note         : {rep.cpu['sku_note']}",
        "ISA supported    : " + " ".join(k for k, v in rep.cpu["isa_supported"].items() if v)
        + " | absent: " + " ".join(k for k, v in rep.cpu["isa_supported"].items() if not v),
        f"SIMD used (numpy): {rep.simd.get('numpy', {}).get('dispatch_targets_active')}",
        f"SIMD used (torch): ATen capability {rep.simd.get('torch', {}).get('aten_cpu_capability')}; "
        f"AVX512_VNNI used: {rep.simd['avx512_vnni_used']} ({rep.simd['avx512_vnni_note']})",
        f"RAM              : total {rep.ram_total_bytes/2**30:.1f} GiB, available {rep.ram_available_bytes/2**30:.1f} GiB",
        f"Disk (cwd)       : free {rep.disk_free_bytes/2**30:.1f} GiB of {rep.disk_total_bytes/2**30:.1f} GiB",
        f"GPU              : {gpu}",
        f"GDAL             : {rep.gdal_version} (bundled with rasterio); gdal CLI on PATH: {rep.gdal_cli}",
        f"Internet         : {rep.internet}",
        f"Docker           : {rep.docker}",
        f"Profile          : {rep.recommended_profile} -> {rep.profile_settings}",
        "Libraries        : " + ", ".join(f"{k}={v}" for k, v in rep.libraries.items()),
    ]
    return "\n".join(lines)


def torch_device(requested: str = "auto") -> str:
    """Return the device that will *actually* be used. Never silently claims cuda."""
    try:
        import torch
    except Exception:
        return "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("device=cuda requested but torch.cuda.is_available() is False")
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    return requested
