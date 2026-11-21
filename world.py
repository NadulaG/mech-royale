"""Static map model: terrain, buildings, bridges rasterised onto the terrain's 1 m vertex grid.

Grid index (i, j) is the world point x = minX + i, y = minY + j (same as terrain.i16).
  ground[j, i]  terrain height
  top[j, i]     highest solid surface (terrain or building roof; bridges excluded)
  bbase/btop    bridge deck interval (nan where no bridge)
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np


def _poly_mask(xs: np.ndarray, ys: np.ndarray, flat) -> np.ndarray:
    """Even-odd point-in-polygon for grid points (xs: (W,), ys: (H,)) -> bool (H, W)."""
    px = np.asarray(flat[0::2], dtype=np.float64)
    py = np.asarray(flat[1::2], dtype=np.float64)
    n = len(px)
    X = xs[None, :]
    Y = ys[:, None]
    inside = np.zeros((len(ys), len(xs)), dtype=bool)
    for k in range(n):
        x1, y1 = px[k], py[k]
        x2, y2 = px[(k + 1) % n], py[(k + 1) % n]
        if y1 == y2:
            continue
        cond = (y1 > Y) != (y2 > Y)  # (H,1)
        rows = np.nonzero(cond[:, 0])[0]
        if len(rows) == 0:
            continue
        yr = ys[rows][:, None]
        xint = x1 + (yr - y1) * (x2 - x1) / (y2 - y1)  # (r,1)
        inside[rows] ^= X < xint
    return inside


class World:
    def __init__(self, map_dir: str, map_files=None):
        t0 = time.time()
        wpath = os.path.join(map_dir, "v2", "world.json")
        if map_files:
            for f in map_files:
                if f.endswith("world.json"):
                    wpath = os.path.join(map_dir, f)
        with open(wpath) as fh:
            w = json.load(fh)
        ter = w["terrain"]
        self.minX = float(ter["minX"])
        self.minY = float(ter["minY"])
        self.step = float(ter.get("step", 1.0))
        self.nx = int(ter["nx"])
        self.ny = int(ter["ny"])
        scale = float(ter.get("scale", 0.01))
        tpath = os.path.join(os.path.dirname(wpath), ter.get("file", "terrain.i16"))
        raw = np.fromfile(tpath, dtype="<i2")
        self.ground = (raw.astype(np.float32) * scale).reshape(self.ny, self.nx)
        b = w["bounds"]
        self.bounds = (b["minX"], b["maxX"], b["minY"], b["maxY"])

        top = self.ground.copy()
        bbase = np.full_like(top, np.nan)
        btop = np.full_like(top, np.nan)
        xs_all = self.minX + np.arange(self.nx) * self.step
        ys_all = self.minY + np.arange(self.ny) * self.step
        self.prisms = []
        for p in w["prisms"]:
            ring = p["ring"]
            rx, ry = ring[0::2], ring[1::2]
            i0 = max(0, int(math.floor((min(rx) - self.minX) / self.step)))
            i1 = min(self.nx - 1, int(math.ceil((max(rx) - self.minX) / self.step)))
            j0 = max(0, int(math.floor((min(ry) - self.minY) / self.step)))
            j1 = min(self.ny - 1, int(math.ceil((max(ry) - self.minY) / self.step)))
            if i1 < i0 or j1 < j0:
                continue
            xs = xs_all[i0:i1 + 1]
            ys = ys_all[j0:j1 + 1]
            m = _poly_mask(xs, ys, ring)
            holes = p.get("holes") or []
            if holes and isinstance(holes[0], (int, float)):
                holes = [holes]
            for h in holes:
                if len(h) >= 6:
                    m &= ~_poly_mask(xs, ys, h)
            if not m.any():
                continue
            self.prisms.append((p["kind"], p["base"], p["top"], (i0, i1, j0, j1)))
            if p["kind"] == "bridge":
                sb = bbase[j0:j1 + 1, i0:i1 + 1]
                st = btop[j0:j1 + 1, i0:i1 + 1]
                sb[m] = np.fmin(sb[m], p["base"])
                st[m] = np.fmax(st[m], p["top"])
            else:
                st = top[j0:j1 + 1, i0:i1 + 1]
                st[m] = np.maximum(st[m], p["top"])
        self.top = top
        self.bbase = bbase
        self.btop = btop
        self.has_bridge = ~np.isnan(btop)
        # walking surface: roof/terrain, or bridge deck when the deck is above that
        surf = top.copy()
        hb = self.has_bridge
        surf[hb] = np.maximum(surf[hb], btop[hb])
        self.surf = surf
        # solid ceiling for LOS: anything below `top` is solid; bridge decks are solid inside [bbase, btop]
        self.bbase_f = np.where(hb, bbase, np.inf).astype(np.float32)
        self.btop_f = np.where(hb, btop, -np.inf).astype(np.float32)
        self.build_s = time.time() - t0

    # ------------------------------------------------------------------ queries
    def idx(self, x: float, y: float):
        i = int(round((x - self.minX) / self.step))
        j = int(round((y - self.minY) / self.step))
        return min(max(i, 0), self.nx - 1), min(max(j, 0), self.ny - 1)

    def ground_z(self, x: float, y: float) -> float:
        fx = (x - self.minX) / self.step
        fy = (y - self.minY) / self.step
        i = min(max(int(math.floor(fx)), 0), self.nx - 2)
        j = min(max(int(math.floor(fy)), 0), self.ny - 2)
        tx = min(max(fx - i, 0.0), 1.0)
        ty = min(max(fy - j, 0.0), 1.0)
        g = self.ground
        a = g[j, i] * (1 - tx) + g[j, i + 1] * tx
        c = g[j + 1, i] * (1 - tx) + g[j + 1, i + 1] * tx
        return float(a * (1 - ty) + c * ty)

    def top_z(self, x: float, y: float) -> float:
        i, j = self.idx(x, y)
        return float(self.top[j, i])

    def surf_z(self, x: float, y: float) -> float:
        i, j = self.idx(x, y)
        return float(self.surf[j, i])

    def surface_below(self, x: float, y: float, z: float) -> float:
        """Height of the surface a mech with feet at z would stand on here."""
        i, j = self.idx(x, y)
        t = float(self.top[j, i])
        if self.has_bridge[j, i]:
            bt = float(self.btop[j, i])
            if z >= bt - 1.5:
                return max(t, bt)
        return t

    def inside_bounds(self, x: float, y: float, margin: float = 0.0) -> bool:
        b = self.bounds
        return b[0] + margin <= x <= b[1] - margin and b[2] + margin <= y <= b[3] - margin

    def blocked_frac(self, a, b, step: float = 1.0) -> float:
        """Fraction along segment a->b of the first solid sample (1.0 = clear)."""
        ax, ay, az = a
        bx, by, bz = b
        d = math.sqrt((bx - ax) ** 2 + (by - ay) ** 2 + (bz - az) ** 2)
        n = max(2, int(d / step) + 1)
        t = np.linspace(0.0, 1.0, n, dtype=np.float32)[1:-1]
        if len(t) == 0:
            return 1.0
        xs = ax + (bx - ax) * t
        ys = ay + (by - ay) * t
        zs = az + (bz - az) * t
        ii = np.clip(np.rint((xs - self.minX) / self.step).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint((ys - self.minY) / self.step).astype(np.int32), 0, self.ny - 1)
        solid = (zs < self.top[jj, ii]) | ((zs > self.bbase_f[jj, ii]) & (zs < self.btop_f[jj, ii]))
        k = np.argmax(solid)
        if not solid[k]:
            return 1.0
        return float(t[k])

    def los(self, a, b, step: float = 1.0) -> bool:
        return self.blocked_frac(a, b, step) >= 1.0
