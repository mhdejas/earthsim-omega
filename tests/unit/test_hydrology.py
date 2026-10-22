import numpy as np

from earthsim import hydro_core as hc
from earthsim import hydrology as hy
from tests.fixtures import synthetic as syn


def _route(z, ocean=None):
    ocean = np.zeros(z.shape, bool) if ocean is None else ocean
    zc, seed, raised = hc.priority_flood_epsilon(z, ocean)
    fd = hc.d8_flowdir(zc, ocean | seed, 30.0)
    return zc, seed, fd, ocean


def test_priority_flood_fills_pit_and_leaves_no_sinks():
    z = syn.with_pit(syn.tilted_plane(), 15, 20, depth=5.0)
    zc, seed, fd, ocean = _route(z)
    assert zc[15, 20] > z[15, 20] + 4.0  # pit raised
    land = ~ocean
    assert ((fd == 0) & land & ~seed).sum() == 0  # every non-seed cell has a receiver
    assert np.all(zc >= z - 1e-12)  # filling never lowers terrain


def test_flow_is_strictly_downhill_and_acyclic():
    z = syn.with_pit(syn.v_valley(), 10, 15, depth=4.0)
    zc, seed, fd, _ = _route(z)
    nxt, _ = hy.downstream_index(fd)
    zf = zc.ravel()
    moving = nxt != np.arange(nxt.size)
    assert np.all(zf[nxt[moving]] < zf[moving])
    hy.label_terminals(nxt)  # raises on cycles


def test_accumulation_conserves_cells():
    z = syn.v_valley(h=31, w=31)
    zc, seed, fd, _ = _route(z)
    acc = hc.accumulation(zc, fd)
    nxt, _ = hy.downstream_index(fd)
    term = np.flatnonzero(nxt == np.arange(nxt.size))
    assert abs(acc.ravel()[term].sum() - z.size) < 1e-6  # every cell counted exactly once at an outlet
    # the valley outlet (bottom centre) collects most of the domain
    assert acc[-1, 15] > 0.5 * z.size


def test_tilted_plane_flows_west():
    z = syn.tilted_plane(slope_x=1.0, slope_y=0.0)
    zc, seed, fd, _ = _route(z)
    assert np.all(fd[1:-1, 1:-1] == 16)  # ESRI 16 = west


def test_ocean_seeding_drains_to_sea():
    z = syn.tilted_plane()
    ocean = np.zeros(z.shape, bool)
    ocean[:, :3] = True
    z[ocean] = 0.0
    zc, seed, fd, _ = _route(z, ocean)
    aoi = np.zeros(z.shape, bool)
    aoi[5:25, 10:35] = True
    labels, rows, summ = hy.basin_catalog(fd, ocean, aoi, z, syn.grid(), min_aoi_km2=0.0)
    assert summ["basins_draining_district"] > 0
    assert summ["internal_sink_basins"] == 0


def test_hand_zero_on_drainage_and_nonnegative():
    z = syn.v_valley()
    zc, seed, fd, ocean = _route(z)
    acc = hc.accumulation(zc, fd)
    streams = acc >= 20
    h, _ = hc.hand_from_routing(zc, fd, streams)
    assert np.allclose(h[streams], 0)
    assert np.nanmin(h) >= -1e-6
    # HAND increases away from the valley axis; edge-seed cells that never reach a stream are NaN (undefined)
    assert h[15, 2] > h[15, 10] > h[15, 15] - 1e-9
    assert np.isnan(h[15, 0])


def test_basin_catalog_flags_truncation_at_domain_edge():
    z = syn.tilted_plane(slope_x=1.0, slope_y=0.0)  # drains off the west edge, no ocean
    zc, seed, fd, ocean = _route(z)
    aoi = np.ones(z.shape, bool)
    _, rows, summ = hy.basin_catalog(fd, ocean, aoi, z, syn.grid(), min_aoi_km2=0.0)
    assert summ["truncated_basins"] == summ["basins_draining_district"] > 0


def test_priority_flood_4_connectivity_opens_diagonal_only_outlets():
    """A cell whose only lower neighbour is diagonal is a pit for a 4-face solver; 4-conn PF must raise it."""
    from earthsim.hydro_core import priority_flood_epsilon

    z = np.full((5, 5), 10.0)
    z[2, 2] = 5.0  # centre
    z[3, 3] = 1.0  # diagonal-only lower neighbour
    z[4, 4] = 0.0
    ocean = np.zeros((5, 5), bool)
    ocean[4, 4] = True
    z8, _, _ = priority_flood_epsilon(z, ocean, connectivity=8)
    z4, _, _ = priority_flood_epsilon(z, ocean, connectivity=4)
    face = [z4[1, 2], z4[3, 2], z4[2, 1], z4[2, 3]]
    assert z8[2, 2] == 5.0  # D8 sees a diagonal outlet: not raised
    assert min(face) < z4[2, 2]  # 4-conn: centre now has a strictly lower face neighbour
    land = ~ocean
    for i in range(5):
        for j in range(5):
            if not land[i, j] or i in (0, 4) or j in (0, 4):
                continue
            nb = [z4[i - 1, j], z4[i + 1, j], z4[i, j - 1], z4[i, j + 1]]
            assert min(nb) < z4[i, j]
