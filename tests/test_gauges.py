"""Unit test (synthetic terrain, code check only): flow counted out of a gauge's D4 catchment must equal
rain on the catchment minus the water still stored in it."""
import numpy as np

from earthsim import hydraulics as hy
from earthsim.gauges import _catchment, _d4, _upstream_area


def test_catchment_outflow_closes_water_balance():
    ny, nx, dx = 40, 30, 100.0
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 0.02 * (ny - yy) * dx / 10 + 0.5 * np.abs(xx - nx // 2) + 1e-4 * xx  # valley draining to row ny-1
    ocean = np.zeros((ny, nx), bool)
    ocean[-1, :] = True
    rec = _d4(z, ocean)
    acc = _upstream_area(rec, z)
    g = (ny - 6, nx // 2)
    assert acc[g[0] * nx + g[1]] > 100
    U = _catchment(rec, g[0] * nx + g[1], ny * nx).reshape(ny, nx)
    a, b = U[:, :-1], U[:, 1:]
    fx = [(i, j, 1.0) for i, j in zip(*np.nonzero(a & ~b))] + [(i, j, -1.0) for i, j in zip(*np.nonzero(~a & b))]
    a, b = U[:-1, :], U[1:, :]
    fy = [(i, j, 1.0) for i, j in zip(*np.nonzero(a & ~b))] + [(i, j, -1.0) for i, j in zip(*np.nonzero(~a & b))]
    fx, fy = np.array(fx).reshape(-1, 3), np.array(fy).reshape(-1, 3)
    out = [0.0]

    def obs(t, dt, h, qx, qy):
        v = (qx[fx[:, 0].astype(int), fx[:, 1].astype(int)] * fx[:, 2]).sum()
        v += (qy[fy[:, 0].astype(int), fy[:, 1].astype(int)] * fy[:, 2]).sum()
        out[0] += v * dx * dt

    rate = 50 / 1000 / 3600  # 50 mm/h for 2 h, then 2 h of drainage
    r = hy.run(z, hy.SolverConfig(dx=dx, manning=0.05), 4 * 3600.0, rain_rate=lambda t: rate if t < 7200 else 0.0,
               ocean=ocean, observer=obs)
    rain_in = rate * 7200 * U.sum() * dx * dx
    stored = r.h_final[U].sum() * dx * dx
    assert out[0] > 0.3 * rain_in
    assert abs(rain_in - stored - out[0]) / rain_in < 1e-3
