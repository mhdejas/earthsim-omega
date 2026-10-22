"""EARTHSIM-Omega command-line interface (click)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .config import load_settings, parse_overrides


def _settings(ctx: click.Context):
    return ctx.obj["settings"]


def _echo_json(obj) -> None:
    click.echo(json.dumps(obj, indent=2, default=str))


def _fail(msg: str, code: int = 2) -> None:
    click.secho(f"ERROR: {msg}", fg="red", err=True)
    sys.exit(code)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--config", "config_path", type=click.Path(dir_okay=False), default=None,
              help="YAML config (default configs/default.yaml or $EARTHSIM_CONFIG)")
@click.option("--set", "overrides", multiple=True, metavar="SECTION.KEY=VALUE",
              help="Override a config value, e.g. --set compute.workers=2 (repeatable)")
@click.pass_context
def main(ctx: click.Context, config_path: str | None, overrides: tuple[str, ...]) -> None:
    """EARTHSIM-Omega regional geospatial digital-twin prototype."""
    try:
        s = load_settings(config_path, parse_overrides(overrides))
    except Exception as e:  # pydantic ValidationError or YAML error
        _fail(f"invalid configuration: {e}")
    ctx.obj = {"settings": s}


# ------------------------------------------------------------------ doctor
@main.command()
@click.option("--out", type=click.Path(), default=None, help="Write JSON hardware report here")
@click.option("--no-internet-check", is_flag=True)
@click.pass_context
def doctor(ctx, out, no_internet_check):
    """Detect hardware/software and recommend a resource profile."""
    from .hardware import detect, summary_text
    from .provenance import atomic_write_text

    s = _settings(ctx)
    rep = detect(check_internet=not no_internet_check, path=str(s.root))
    click.echo(summary_text(rep))
    out = Path(out) if out else s.p.runs_dir / "hardware_report.json"
    atomic_write_text(out, json.dumps(rep.to_dict(), indent=2, default=str))
    click.echo(f"\nhardware report written: {out}")
    try:
        from .catalog import Catalog

        Catalog(s.p.manifest, s.p.raw_dir)
        click.echo("manifest: OK")
    except Exception as e:
        click.secho(f"manifest problem: {e}", fg="yellow")


# -------------------------------------------------------------------- data
@main.group()
def data():
    """Dataset catalog: list / inspect / validate / download / verify / clean."""


def _catalog(s):
    from .catalog import Catalog

    return Catalog(s.p.manifest, s.p.raw_dir, s.p.processed_dir)


@data.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def data_list(ctx, as_json):
    rows = _catalog(_settings(ctx)).status()
    if as_json:
        return _echo_json(rows)
    for r in rows:
        mark = "OK " if r["available"] else "-- "
        click.echo(f"{mark}{r['id']:<34} {r['data_class']:<18} files={r['files']:<3} "
                   f"{r['bytes']/1e6:8.1f} MB  mode={r['mode']}")


@data.command("download")
@click.argument("dataset_id")
@click.option("--mode", type=click.Choice(["auto", "local", "download"]), default="auto")
@click.option("--force", is_flag=True)
@click.option("--dry-run", is_flag=True, help="Show what would be acquired")
@click.pass_context
def data_download(ctx, dataset_id, mode, force, dry_run):
    """Acquire a dataset (LOCAL mode uses existing files; DOWNLOAD fetches public data)."""
    from .catalog import CatalogError

    c = _catalog(_settings(ctx))
    try:
        d = c.get(dataset_id)
        if dry_run:
            return _echo_json({"dataset": dataset_id, "adapter": d["adapter"], "mode": mode,
                               "urls": d.get("urls"), "local_candidates": d.get("local_candidates"),
                               "dest": str(c.ds_dir(dataset_id))})
        acq = c.acquire(dataset_id, mode=mode, force=force)
    except CatalogError as e:
        _fail(str(e))
    click.echo(f"acquired {dataset_id} ({acq['mode']}): {len(acq['files'])} files")


@data.command("verify")
@click.argument("dataset_id")
@click.pass_context
def data_verify(ctx, dataset_id):
    """Verify recorded SHA-256 checksums."""
    from .catalog import CatalogError

    try:
        r = _catalog(_settings(ctx)).verify(dataset_id)
    except CatalogError as e:
        _fail(str(e))
    _echo_json(r)
    if not r["all_ok"]:
        sys.exit(1)


@data.command("validate")
@click.argument("dataset_id")
@click.pass_context
def data_validate(ctx, dataset_id):
    """Checksums + open every file + CRS/nodata/geometry checks."""
    from .catalog import CatalogError

    try:
        r = _catalog(_settings(ctx)).validate(dataset_id)
    except CatalogError as e:
        _fail(str(e))
    _echo_json({k: v for k, v in r.items() if k != "files"} | {"files_checked": len(r["files"])})
    if not r["all_ok"]:
        sys.exit(1)


@data.command("inspect")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def data_inspect(ctx, path):
    """Inspect raster (.tif) or vector (.geojson/.gpkg/.parquet) metadata."""
    from .catalog import inspect_raster, inspect_vector

    p = Path(path)
    try:
        info = inspect_raster(p) if p.suffix.lower() in (".tif", ".tiff", ".vrt") else inspect_vector(p)
    except Exception as e:
        _fail(f"cannot open {p}: {e}")
    _echo_json(info)


@data.command("clean")
@click.argument("targets", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="Actually delete (default is a dry run)")
@click.pass_context
def data_clean(ctx, targets, yes):
    """Remove explicitly named derived caches under data/processed (never raw data)."""
    removed = _catalog(_settings(ctx)).clean(list(targets), dry_run=not yes)
    click.echo(("removed: " if yes else "would remove: ") + ", ".join(removed or ["nothing"]))


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------- run
STAGES = ["terrain", "s2_acquire", "s2_indices", "landcover", "s1_acquire", "flood_obs", "flood_susceptibility",
          "hindcast", "hindcast_eval", "benchmarks"]


@main.command("run")
@click.argument("stage", type=click.Choice(STAGES))
@click.option("--tag", default="base", help="hindcast: output tag")
@click.option("--runoff-c", type=float, default=0.7, help="hindcast: runoff coefficient (0-1]")
@click.option("--n-land", type=float, default=0.08, help="hindcast: Manning n off-channel")
@click.option("--factor", type=int, default=8, help="hindcast: aggregation factor from the 30 m DEM (8 = 240 m)")
@click.pass_context
def run_stage(ctx, stage, tag, runoff_c, n_land, factor):
    """Run one pipeline stage (results + provenance under data/processed/<stage>/)."""
    import time

    from . import pipelines as P

    s = _settings(ctx)
    if not (0 < runoff_c <= 1):
        _fail("--runoff-c must be in (0, 1]")
    if factor < 1:
        _fail("--factor must be >= 1")
    t = time.time()
    if stage == "hindcast":
        from .scenario import run_hindcast

        rain = s.p.raw_dir / "era5_rain_kozhikode_2018_2019" / "era5_precip_2019-08-01_2019-08-31.json"
        if not rain.exists():
            _fail("ERA5 rain not acquired: earthsim data download era5_rain_kozhikode_2018_2019")
        r = run_hindcast(s.p.processed_dir / "terrain", rain, s.p.processed_dir / "hydraulics", factor=factor,
                         runoff_c=runoff_c, n_land=n_land, tag=tag, progress=click.echo)
    elif stage == "benchmarks":
        from .benchmarks import run_all

        r = run_all(s.root / "benchmarks" / "results")
    else:
        fn = getattr(P, f"stage_{stage}")
        r = fn(s)
    click.echo(json.dumps(r, indent=2, default=str)[:4000])
    click.secho(f"{stage} finished in {time.time() - t:.1f} s", fg="green")


@main.command()
@click.option("--host", default=None)
@click.option("--port", type=int, default=None)
@click.pass_context
def serve(ctx, host, port):
    """Serve the API and web map (default 127.0.0.1:8000)."""
    import uvicorn

    from .api import create_app

    s = _settings(ctx)
    uvicorn.run(create_app(s), host=host or s.api.host, port=port or s.api.port, log_level="info")
