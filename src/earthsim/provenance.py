"""Provenance records, checksums, structured logging and atomic publication."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "earthsim.provenance/1.0"

_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL", "AUTH")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def software_versions() -> dict[str, str | None]:
    from .hardware import GEO_LIBS, _version

    from . import __version__

    v = {m: _version(m) for m in GEO_LIBS}
    v["earthsim"] = __version__
    v["python"] = sys.version.split()[0]
    v["platform"] = platform.platform()
    try:
        import rasterio

        v["gdal"] = rasterio.__gdal_version__
    except Exception:
        pass
    return v


def redact_env() -> dict[str, str]:
    """Environment snapshot with anything secret-looking removed (never logged raw)."""
    out = {}
    for k, v in os.environ.items():
        if k.startswith(("EARTHSIM_", "GDAL_", "PROJ_", "CPL_")):
            out[k] = "<redacted>" if any(h in k.upper() for h in _SECRET_HINTS) else v
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": utcnow(), "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        extra = getattr(record, "extra_fields", None)
        if extra:
            d.update(extra)
        return json.dumps(d, default=str)


def get_logger(name: str = "earthsim", level: str = "INFO", json_logs: bool = True) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    log.setLevel(level)
    return log


def log_event(log: logging.Logger, msg: str, **fields: Any) -> None:
    log.info(msg, extra={"extra_fields": fields})


class RunRecord:
    """Machine-readable record linking outputs to inputs, config, software and run id."""

    def __init__(self, process: str, config: dict[str, Any] | None = None, run_id: str | None = None):
        self.run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%S')}_{process}_{uuid.uuid4().hex[:6]}"
        self.data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "run_id": self.run_id,
            "process": process,
            "started_utc": utcnow(),
            "finished_utc": None,
            "status": "running",
            "config": config or {},
            "software": software_versions(),
            "environment": redact_env(),
            "inputs": [],
            "outputs": [],
            "events": [],
            "metrics": {},
            "data_classification": None,
        }
        self._t0 = time.perf_counter()

    def add_input(self, path: str | Path, dataset_id: str | None = None, checksum: bool = True, **meta: Any) -> None:
        p = Path(path)
        rec = {"path": str(p), "dataset_id": dataset_id, **meta}
        if checksum and p.is_file():
            rec["sha256"] = sha256_file(p)
            rec["bytes"] = p.stat().st_size
        self.data["inputs"].append(rec)

    def add_output(self, path: str | Path, kind: str, checksum: bool = True, **meta: Any) -> None:
        p = Path(path)
        rec = {"path": str(p), "kind": kind, **meta}
        if checksum and p.is_file():
            rec["sha256"] = sha256_file(p)
            rec["bytes"] = p.stat().st_size
        self.data["outputs"].append(rec)

    def event(self, msg: str, **fields: Any) -> None:
        self.data["events"].append({"ts": utcnow(), "msg": msg, **fields})

    def finish(self, status: str = "succeeded", **metrics: Any) -> dict[str, Any]:
        self.data["status"] = status
        self.data["finished_utc"] = utcnow()
        self.data["wall_seconds"] = round(time.perf_counter() - self._t0, 3)
        self.data["metrics"].update(metrics)
        return self.data

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        atomic_write_text(path, json.dumps(self.data, indent=2, default=str))
        return path


def atomic_write_text(path: str | Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


@contextmanager
def atomic_output_dir(final: str | Path):
    """Write into a temp dir, then rename into place only on success."""
    final = Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=final.parent, prefix=f".{final.name}.partial_"))
    try:
        yield tmp
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if final.exists():
        shutil.rmtree(final)
    os.replace(tmp, final)
