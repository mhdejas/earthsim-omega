"""Validated, YAML-driven configuration with environment-variable overrides.

Precedence (highest first): explicit CLI overrides > EARTHSIM_* environment
variables > YAML file > model defaults.  Environment variables use a double
underscore for nesting, e.g. ``EARTHSIM_COMPUTE__WORKERS=2``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegionConfig(_Strict):
    name: str = "Kozhikode"
    boundary_dataset: str = "aoi_kozhikode_geoboundaries"
    discovery_crs: str = "EPSG:4326"
    analysis_crs: str = "EPSG:32643"
    # Buffer (metres, in analysis CRS) applied when fetching source data so that
    # edge pixels and drainage crossing the boundary are not truncated.
    fetch_buffer_m: float = Field(2000.0, ge=0, le=50_000)

    @field_validator("analysis_crs", "discovery_crs")
    @classmethod
    def _valid_crs(cls, v: str) -> str:
        from pyproj import CRS

        CRS.from_user_input(v)  # raises a clear pyproj error if invalid
        return v

    @model_validator(mode="after")
    def _projected(self) -> RegionConfig:
        from pyproj import CRS

        if not CRS.from_user_input(self.analysis_crs).is_projected:
            raise ValueError(
                f"analysis_crs {self.analysis_crs} is geographic; metric areas/distances need a projected CRS"
            )
        return self


class PathsConfig(_Strict):
    data_dir: Path = Path("data")
    raw_dir: Path = Path("data/raw")
    processed_dir: Path = Path("data/processed")
    fixtures_dir: Path = Path("data/fixtures")
    runs_dir: Path = Path("experiments/results")
    models_dir: Path = Path("models")
    benchmarks_dir: Path = Path("benchmarks/results")
    manifest: Path = Path("configs/datasets.yaml")
    jobs_db: Path = Path("data/jobs.sqlite")

    def resolve(self, root: Path) -> PathsConfig:
        vals = {k: (v if Path(v).is_absolute() else (root / v)) for k, v in self.model_dump().items()}
        return PathsConfig(**vals)


class ComputeConfig(_Strict):
    profile: Literal["auto", "development", "workstation", "high_memory", "hpc"] = "auto"
    workers: int | None = Field(None, ge=1, le=256)
    tile_size: int = Field(1024, ge=64, le=16384)
    max_memory_fraction: float = Field(0.5, gt=0.05, le=0.9)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    seed: int = 42


class ProcessingConfig(_Strict):
    dem_resolution_m: float = Field(30.0, gt=0)
    optical_resolution_m: float = Field(20.0, gt=0)
    stream_threshold_km2: float = Field(1.0, gt=0)
    # Hydrology needs whole upstream catchments, not just the district: wider DEM domain.
    terrain_buffer_m: float = Field(45000.0, ge=0)
    composite_start: str = "2025-01-01"
    composite_end: str = "2025-03-31"
    max_scene_cloud_pct: float = Field(30.0, ge=0, le=100)
    max_items: int = Field(8, ge=1, le=64)


class ApiConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    max_request_bytes: int = Field(1_000_000, ge=1024)
    job_timeout_s: int = Field(3600, ge=10)
    max_concurrent_jobs: int = Field(2, ge=1, le=16)


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_logs: bool = True


class Settings(_Strict):
    region: RegionConfig = RegionConfig()
    paths: PathsConfig = PathsConfig()
    compute: ComputeConfig = ComputeConfig()
    processing: ProcessingConfig = ProcessingConfig()
    api: ApiConfig = ApiConfig()
    logging: LoggingConfig = LoggingConfig()
    root: Path = PROJECT_ROOT

    @property
    def p(self) -> PathsConfig:
        return self.paths.resolve(self.root)


def _deep_merge(base: dict[str, Any], upd: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(prefix: str = "EARTHSIM_") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in os.environ.items():
        if not key.startswith(prefix) or key == "EARTHSIM_CONFIG":
            continue
        parts = key[len(prefix):].lower().split("__")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = yaml.safe_load(val)
    return out


def parse_overrides(items: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Parse ``section.key=value`` strings into a nested dict."""
    out: dict[str, Any] = {}
    for it in items:
        if "=" not in it:
            raise ValueError(f"Override '{it}' must look like section.key=value")
        k, v = it.split("=", 1)
        cur = out
        parts = k.strip().split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = yaml.safe_load(v)
    return out


def load_settings(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    path = path or os.environ.get("EARTHSIM_CONFIG") or (PROJECT_ROOT / "configs" / "default.yaml")
    data: dict[str, Any] = {}
    path = Path(path)
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    elif os.environ.get("EARTHSIM_CONFIG"):
        raise FileNotFoundError(f"EARTHSIM_CONFIG points to missing file: {path}")
    data = _deep_merge(data, _env_overrides())
    if overrides:
        data = _deep_merge(data, overrides)
    return Settings(**data)
