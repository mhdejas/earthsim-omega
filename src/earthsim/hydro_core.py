"""Hydrological conditioning and D8 routing kernels (numba, float64).

Priority-Flood + epsilon (Barnes, Lehman & Mulla 2014, Computers & Geosciences
62:117-127, Algorithm 3): starting from outlet seeds (ocean cells adjacent to
land, and the domain border), cells are visited in increasing elevation order;
any neighbour not higher than the current cell is raised to the next
representable float64 above it. Result: every non-seed cell has a strictly
lower neighbour, so D8 steepest descent has no pits or flats and every cell
drains to a seed. This guarantee is tested in tests/unit/test_hydrology.py.

Why not pysheds' conditioning: on the Kozhikode domain pysheds 0.5
(fill_depressions + resolve_flats, float32 input) left 83 flat and 954 pit
cells on main channels, terminating the Chaliyar system inland (logged in
data/processed/terrain/terrain_summary.json -> pysheds_crosscheck).
"""

from __future__ import annotations

import heapq

import numba as nb
import numpy as np

# ESRI D8 codes, same order as offsets below.
D8_CODES = np.array([64, 128, 1, 2, 4, 8, 16, 32], dtype=np.int16)  # N NE E SE S SW W NW
DR = np.array([-1, -1, 0, 1, 1, 1, 0, -1], dtype=np.int64)
DC = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int64)
DIST = np.array([1.0, np.sqrt(2), 1.0, np.sqrt(2), 1.0, np.sqrt(2), 1.0, np.sqrt(2)])


@nb.njit(cache=True)
def _priority_flood_eps(z, seed, closed, step):
    h, w = z.shape
    heap = [(0.0, np.int64(0))]
    heap.pop()
    pit = np.empty(h * w, dtype=np.int64)
    ph = 0
    pt = 0
    for i in range(h):
        for j in range(w):
            if seed[i, j]:
                heapq.heappush(heap, (z[i, j], np.int64(i * w + j)))
                closed[i, j] = True
    raised = 0
    while len(heap) > 0 or ph < pt:
        if ph < pt:
            c = pit[ph]
            ph += 1
        else:
            c = heapq.heappop(heap)[1]
        ci = c // w
        cj = c % w
        zc = z[ci, cj]
        for k in range(0, 8, step):
            ni = ci + DR[k]
            nj = cj + DC[k]
            if ni < 0 or ni >= h or nj < 0 or nj >= w or closed[ni, nj]:
                continue
            closed[ni, nj] = True
            if z[ni, nj] <= zc:
                z[ni, nj] = np.nextafter(zc, np.inf)
                raised += 1
                pit[pt] = ni * w + nj
                pt += 1
            else:
                heapq.heappush(heap, (z[ni, nj], np.int64(ni * w + nj)))
    return raised


def priority_flood_epsilon(dem: np.ndarray, ocean: np.ndarray, connectivity: int = 8
                           ) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (conditioned float64 DEM, seed mask, number of raised cells).

    Seeds = land-adjacent ocean cells + land cells on the raster border.
    Interior ocean cells are closed (excluded) and keep their value.
    ``connectivity=8`` guarantees a strictly lower D8 neighbour (for routing);
    ``connectivity=4`` guarantees a strictly lower *face* neighbour, which a
    4-neighbour finite-volume solver needs: with 8-connectivity a cell whose
    only lower neighbour is diagonal is a closed pit for the solver.
    """
    if connectivity not in (4, 8):
        raise ValueError("connectivity must be 4 or 8")
    z = np.asarray(dem, dtype=np.float64).copy()
    if not np.all(np.isfinite(z[~ocean])):
        raise ValueError("DEM has non-finite land cells; fill or mask them before conditioning")
    from scipy import ndimage

    land = ~ocean
    near_land = ndimage.binary_dilation(land, structure=np.ones((3, 3), bool) if connectivity == 8
                                        else ndimage.generate_binary_structure(2, 1))
    seed = ocean & near_land
    border = np.zeros_like(land)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    seed |= border & land
    closed = ocean & ~seed  # far ocean never visited
    raised = _priority_flood_eps(z, seed, closed, 2 if connectivity == 4 else 1)
    return z, seed, int(raised)


@nb.njit(cache=True)
def _d8(z, terminal, res):
    h, w = z.shape
    fd = np.zeros((h, w), dtype=np.int16)
    for i in range(h):
        for j in range(w):
            if terminal[i, j]:
                continue
            best = 0.0
            bk = -1
            for k in range(8):
                ni = i + DR[k]
                nj = j + DC[k]
                if ni < 0 or ni >= h or nj < 0 or nj >= w:
                    continue
                s = (z[i, j] - z[ni, nj]) / (DIST[k] * res)
                if s > best:
                    best = s
                    bk = k
            if bk >= 0:
                fd[i, j] = D8_CODES[bk]
    return fd


def d8_flowdir(z: np.ndarray, terminal: np.ndarray, res: float) -> np.ndarray:
    """Steepest-descent D8 (ESRI codes); 0 for terminals / cells without a lower neighbour."""
    return _d8(np.asarray(z, np.float64), terminal, float(res))


@nb.njit(cache=True)
def _accumulate(order, fd, w, weights):
    acc = weights.copy()
    for idx in order:
        i = idx // w
        j = idx % w
        code = fd[i, j]
        if code == 0:
            continue
        for k in range(8):
            if D8_CODES[k] == code:
                acc[(i + DR[k]) * w + (j + DC[k])] += acc[idx]
                break
    return acc


def accumulation(z: np.ndarray, fd: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """Upstream-cell count (incl. self) via descending-elevation order.

    Valid because receivers are strictly lower than donors after Priority-Flood+eps.
    """
    h, w = fd.shape
    order = np.argsort(-np.asarray(z, np.float64), axis=None, kind="stable")
    wts = np.ones(h * w) if weights is None else np.asarray(weights, np.float64).ravel()
    return _accumulate(order, fd, w, wts).reshape(h, w)


def hand_from_routing(z: np.ndarray, fd: np.ndarray, drainage: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """HAND = z - z(first drainage cell reached downstream along D8). Returns (hand, target index)."""
    from .hydrology import downstream_index, label_terminals

    nxt, _ = downstream_index(fd, terminal_mask=drainage)
    tgt = label_terminals(nxt)
    zf = np.asarray(z, np.float64).ravel()
    hand = (zf - zf[tgt]).reshape(fd.shape)
    reached = drainage.ravel()[tgt].reshape(fd.shape)
    hand = np.where(reached, hand, np.nan)
    return hand.astype("float32"), tgt


@nb.njit(cache=True)
def _carve_diagonals(z, fd):
    h, w = z.shape
    carved = 0
    for i in range(h):
        for j in range(w):
            code = fd[i, j]
            if code == 0:
                continue
            for k in range(1, 8, 2):  # diagonal directions only
                if D8_CODES[k] != code:
                    continue
                ri = i + DR[k]
                rj = j + DC[k]
                ai, aj = ri, j  # face cell sharing the row of the receiver
                bi, bj = i, rj  # face cell sharing the column of the receiver
                if z[ai, aj] <= z[bi, bj]:
                    ci, cj = ai, aj
                else:
                    ci, cj = bi, bj
                mid = 0.5 * (z[i, j] + z[ri, rj])
                if z[ci, cj] > mid:
                    z[ci, cj] = mid
                    carved += 1
    return carved


def carve_diagonal_steps(z8: np.ndarray, fd: np.ndarray) -> tuple[np.ndarray, int]:
    """Make D8 flow paths face-connected: for each diagonal step donor->receiver, lower the lower of the two
    shared face cells to the mid elevation (only ever lowers). Used before 4-connected Priority-Flood so
    diagonal thalwegs are not dammed by the hillslope cells beside them."""
    z = np.asarray(z8, np.float64).copy()
    n = _carve_diagonals(z, fd)
    return z, int(n)
