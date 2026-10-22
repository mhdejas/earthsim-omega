"""Unified dataset catalog: manifest, LOCAL / DOWNLOAD adapters, validation,
metadata inspection, checksum verification and safe cache cleanup."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from .provenance import atomic_write_text, sha256_file, utcnow

ACQ_FILE = "_acquisition.json"
USER_AGENT = "EARTHSIM-Omega/0.1 (research prototype; Kozhikode digital twin)"


class CatalogError(RuntimeError):
    pass


class Catalog:
    def __init__(self, manifest: str | Path, raw_dir: str | Path, processed_dir: str | Path | None = None):
        self.manifest_path = Path(manifest)
        if not self.manifest_path.exists():
            raise CatalogError(f"manifest not found: {manifest}")
        m = yaml.safe_load(self.manifest_path.read_text())
        self.datasets: dict[str, dict[str, Any]] = {d["id"]: d for d in m["datasets"]}
        self.raw_dir = Path(raw_dir)
        self.processed_dir = Path(processed_dir) if processed_dir else None
        # Local-only inputs live outside the repo; ${EARTHSIM_LOCAL_DATA} defaults to the
        # directory that contains the project checkout.
        self.local_data = os.environ.get("EARTHSIM_LOCAL_DATA") or str(self.manifest_path.resolve().parent.parent.parent)

    def _expand(self, c: str) -> Path:
        return Path(str(c).replace("${EARTHSIM_LOCAL_DATA}", self.local_data)).expanduser()

    # ------------------------------------------------------------------ query
    def get(self, ds_id: str) -> dict[str, Any]:
        if ds_id not in self.datasets:
            raise CatalogError(f"unknown dataset '{ds_id}'. Known: {', '.join(self.datasets)}")
        return self.datasets[ds_id]

    def ds_dir(self, ds_id: str) -> Path:
        return self.raw_dir / ds_id

    def acquisition(self, ds_id: str) -> dict[str, Any] | None:
        f = self.ds_dir(ds_id) / ACQ_FILE
        return json.loads(f.read_text()) if f.exists() else None

    def files(self, ds_id: str) -> list[Path]:
        acq = self.acquisition(ds_id)
        if acq:
            return [Path(f["path"]) for f in acq["files"]]
        return []

    def status(self) -> list[dict[str, Any]]:
        rows = []
        for i, d in self.datasets.items():
            acq = self.acquisition(i)
            rows.append({
                "id": i, "title": d["title"], "data_class": d["data_class"], "adapter": d["adapter"],
                "available": bool(acq), "mode": acq.get("mode") if acq else None,
                "files": len(acq["files"]) if acq else 0,
                "bytes": sum(f.get("bytes", 0) for f in acq["files"]) if acq else 0,
                "acquired_utc": acq.get("acquired_utc") if acq else None, "license": d.get("license"),
            })
        return rows

    # ---------------------------------------------------------------- acquire
    def acquire(self, ds_id: str, mode: str = "auto", force: bool = False, **kw: Any) -> dict[str, Any]:
        """mode: 'local' (only existing files), 'download', or 'auto' (local then download)."""
        d = self.get(ds_id)
        if self.acquisition(ds_id) and not force:
            return self.acquisition(ds_id)
        out = self.ds_dir(ds_id)
        out.mkdir(parents=True, exist_ok=True)
        adapter = d["adapter"]
        files: list[dict[str, Any]] = []
        used_mode = None
        extra: dict[str, Any] = {}
        if mode in ("local", "auto") and d.get("local_candidates"):
            files = self._local(d, out)
            used_mode = "local" if files else None
        if not files and mode in ("download", "auto"):
            if adapter == "local":
                raise CatalogError(f"{ds_id} is local-only and no candidate file exists: {d.get('local_candidates')}")
            fn = getattr(self, f"_dl_{adapter}", None)
            if fn is None:
                from . import remote_sensing, sar  # adapters living in domain modules

                fn = {"stac_sentinel2": remote_sensing.download_sentinel2,
                      "pc_sentinel1_rtc": sar.download_sentinel1_rtc}.get(adapter)
                if fn is None:
                    raise CatalogError(f"no download adapter '{adapter}'")
                files, extra = fn(self, d, out, **kw)
            else:
                files, extra = fn(d, out)
            used_mode = "download"
        if not files:
            raise CatalogError(f"could not acquire {ds_id} in mode={mode}")
        if d.get("adapter") == "geoboundaries":
            files, sel = self._select_boundary(d, out, files)
            extra["selection"] = sel
        acq = {"schema": "earthsim.acquisition/1.0", "dataset_id": ds_id, "mode": used_mode,
               "acquired_utc": utcnow(), "files": files, **extra}
        atomic_write_text(out / ACQ_FILE, json.dumps(acq, indent=2, default=str))
        return acq

    def _local(self, d: dict[str, Any], out: Path) -> list[dict[str, Any]]:
        files = []
        for c in d["local_candidates"]:
            p = self._expand(c)
            if p.is_dir() and d.get("glob"):
                cands = sorted(p.glob(d["glob"]))
            elif p.is_file():
                cands = [p]
            else:
                cands = []
            for f in cands:
                sha = sha256_file(f)
                if d.get("expected_sha256") and len(d["local_candidates"]) == 1 and sha != d["expected_sha256"]:
                    raise CatalogError(f"checksum mismatch for local {f}: {sha} != {d['expected_sha256']}")
                # reference original user files in place (never moved, never deleted)
                files.append({"path": str(f), "sha256": sha, "bytes": f.stat().st_size, "origin": "local_existing"})
        return files

    @staticmethod
    def _http_get(url: str, dest: Path, retries: int = 3) -> dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(1, retries + 1):
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=120,
                                  headers={"User-Agent": USER_AGENT}) as r:
                    r.raise_for_status()
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                tmp.replace(dest)
                return {"path": str(dest), "url": url, "sha256": sha256_file(dest), "bytes": dest.stat().st_size,
                        "origin": "download", "retrieved_utc": utcnow()}
            except Exception:
                if attempt == retries:
                    raise
                time.sleep(2 * attempt)
        raise CatalogError("unreachable")

    def _dl_http_files(self, d: dict[str, Any], out: Path):
        return [self._http_get(u, out / Path(u).name) for u in d["urls"]], {}

    def _dl_geoboundaries(self, d: dict[str, Any], out: Path):
        f = self._http_get(d["urls"][0], out / "geoBoundaries-IND-ADM2.geojson")
        if d.get("expected_sha256") and f["sha256"] != d["expected_sha256"]:
            raise CatalogError("downloaded geoBoundaries checksum differs from pinned commit")
        return [f], {}

    def _select_boundary(self, d, out: Path, files):
        import geopandas as gpd

        from .geo import validate_geometry

        src = Path(files[0]["path"])
        gdf = gpd.read_file(src)
        sel = d["select"]
        sub = gdf[gdf[sel["field"]].str.lower() == sel["value"].lower()].copy()
        if len(sub) != 1:
            raise CatalogError(f"boundary selection returned {len(sub)} features")
        val = validate_geometry(sub)
        dest = out / "kozhikode_boundary.geojson"
        sub.to_file(dest, driver="GeoJSON")
        files = [{**files[0], "role": "source"},
                 {"path": str(dest), "sha256": sha256_file(dest), "bytes": dest.stat().st_size, "role": "aoi",
                  "derived_from": str(src)}]
        return files, {"rule": f"{sel['field']} == {sel['value']} (case-insensitive)", "validation": val,
                       "shapeID": sub.iloc[0].get("shapeID")}

    def _dl_overpass(self, d: dict[str, Any], out: Path):
        bbox = "11.10,75.50,11.83,76.17"
        queries = {
            "roads": f'[out:json][timeout:180];way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"]({bbox});out geom;',
            "facilities": f'[out:json][timeout:180];(nwr["amenity"~"^(hospital|clinic|school|college)$"]({bbox}););out center tags;',
            "buildings": f'[out:json][timeout:300];way["building"]({bbox});out center;',
        }
        files = []
        for name, q in queries.items():
            last = None
            attempts = [(ep, k) for k in range(3) for ep in d["urls"]]
            for ep, k in attempts:
                try:
                    r = httpx.post(ep, data={"data": q}, timeout=400, headers={"User-Agent": USER_AGENT})
                    r.raise_for_status()
                    js = r.json()
                    if js.get("remark") and "error" in js["remark"].lower():
                        raise CatalogError(js["remark"])
                    dest = out / f"osm_{name}.json"
                    dest.write_text(json.dumps(js))
                    files.append({"path": str(dest), "url": ep, "query": q, "sha256": sha256_file(dest),
                                  "bytes": dest.stat().st_size, "elements": len(js.get("elements", [])),
                                  "osm_base": js.get("osm3s", {}).get("timestamp_osm_base"),
                                  "origin": "download", "retrieved_utc": utcnow(), "attempt": k + 1})
                    break
                except Exception as e:  # try next mirror
                    last = f"{ep}: {type(e).__name__}: {str(e)[:200]}"
                    time.sleep(min(60, 5 * 2**k))  # exponential backoff
            else:
                raise CatalogError(f"overpass failed for {name}: {last}")
        return files, {"bbox_s_w_n_e": bbox}

    def _dl_open_meteo(self, d: dict[str, Any], out: Path):
        files = []
        for start, end in d["periods"]:
            lats = ",".join(str(p[0]) for p in d["points"])
            lons = ",".join(str(p[1]) for p in d["points"])
            url = (f"{d['urls'][0]}?latitude={lats}&longitude={lons}&start_date={start}&end_date={end}"
                   "&hourly=precipitation&timezone=Asia%2FKolkata&models=era5")
            r = httpx.get(url, timeout=120)
            r.raise_for_status()
            dest = out / f"era5_precip_{start}_{end}.json"
            dest.write_text(r.text)
            files.append({"path": str(dest), "url": url, "sha256": sha256_file(dest), "bytes": dest.stat().st_size,
                          "origin": "download", "retrieved_utc": utcnow()})
        return files, {}

    # ------------------------------------------------------------- validation
    def verify(self, ds_id: str) -> dict[str, Any]:
        acq = self.acquisition(ds_id)
        if not acq:
            raise CatalogError(f"{ds_id} not acquired; run `earthsim data download {ds_id}`")
        res = []
        for f in acq["files"]:
            p = Path(f["path"])
            ok = p.exists() and sha256_file(p) == f["sha256"]
            res.append({"path": str(p), "exists": p.exists(), "checksum_ok": ok})
        return {"dataset_id": ds_id, "all_ok": all(r["checksum_ok"] for r in res), "files": res}

    def validate(self, ds_id: str) -> dict[str, Any]:
        """Checksum + format-level validation (opens every raster/vector file)."""
        rep = self.verify(ds_id)
        issues = []
        for f in rep["files"]:
            p = Path(f["path"])
            if p.suffix.lower() in (".tif", ".tiff"):
                info = inspect_raster(p)
                if info["crs"] is None:
                    issues.append(f"{p.name}: no CRS")
                if info["nodata"] is None:
                    issues.append(f"{p.name}: no nodata value declared (handle explicitly)")
            elif p.suffix.lower() in (".geojson", ".gpkg", ".parquet"):
                info = inspect_vector(p)
                if info["invalid_geometries"]:
                    issues.append(f"{p.name}: {info['invalid_geometries']} invalid geometries")
        rep["issues"] = issues
        return rep

    def clean(self, targets: list[str], dry_run: bool = True) -> list[str]:
        """Remove only explicitly named *derived* cache directories under processed_dir."""
        if self.processed_dir is None:
            raise CatalogError("no processed_dir configured")
        removed = []
        base = self.processed_dir.resolve()
        for t in targets:
            p = (base / t).resolve()
            if base not in p.parents:
                raise CatalogError(f"refusing to delete outside processed dir: {t}")
            if p.exists():
                removed.append(str(p))
                if not dry_run:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
        return removed


def inspect_raster(path: str | Path) -> dict[str, Any]:
    import numpy as np
    import rasterio

    with rasterio.open(path) as src:
        info = {
            "path": str(path), "driver": src.driver, "crs": src.crs.to_string() if src.crs else None,
            "width": src.width, "height": src.height, "count": src.count, "dtypes": list(src.dtypes),
            "res": src.res, "bounds": list(src.bounds), "nodata": src.nodata,
            "transform": list(src.transform)[:6], "overviews": src.overviews(1), "tags": src.tags(),
            "block_shapes": src.block_shapes[:1], "descriptions": list(src.descriptions),
        }
        # sample statistics from the lowest-resolution overview to stay cheap
        fac = max(1, max(src.width, src.height) // 1024)
        a = src.read(1, out_shape=(max(1, src.height // fac), max(1, src.width // fac)), masked=True)
        comp = a.compressed()
        info["sample_stats"] = ({"min": float(comp.min()), "max": float(comp.max()), "mean": float(comp.mean()),
                                 "valid_fraction": float(comp.size / a.size)} if comp.size else {"valid_fraction": 0.0})
        info["sample_stats"]["nan_count"] = int(np.isnan(comp).sum()) if comp.dtype.kind == "f" else 0
    return info


def inspect_vector(path: str | Path) -> dict[str, Any]:
    import geopandas as gpd

    g = gpd.read_parquet(path) if str(path).endswith(".parquet") else gpd.read_file(path)
    return {"path": str(path), "crs": g.crs.to_string() if g.crs else None, "features": len(g),
            "geometry_types": sorted(set(g.geom_type.dropna())), "bounds": list(g.total_bounds),
            "columns": list(g.columns), "invalid_geometries": int((~g.geometry.is_valid).sum()),
            "empty_geometries": int(g.geometry.is_empty.sum())}
