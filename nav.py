"""Coarse A* over a 4 m grid of surface heights, time-sliced across ticks."""
from __future__ import annotations

import heapq
import math
import time

import numpy as np

CELL = 4


class Nav:
    def __init__(self, world, step_up: float = 1.2, max_climb: float = 45.0, margin: float = 6.0):
        self.w = world
        s = world.surf
        ny, nx = s.shape
        W = nx // CELL
        H = ny // CELL
        blk = s[: H * CELL, : W * CELL].reshape(H, CELL, W, CELL)
        hmax = blk.max(axis=(1, 3))
        self.W, self.H = W, H
        self.hmax = hmax
        self.h = hmax.astype(np.float64).ravel().tolist()
        self.step_up = step_up
        self.max_climb = max_climb
        # out-of-bounds margin cells are impassable
        b = world.bounds
        ok = np.ones((H, W), dtype=bool)
        xs = world.minX + (np.arange(W) + 0.5) * CELL
        ys = world.minY + (np.arange(H) + 0.5) * CELL
        ok &= ((xs > b[0] + margin) & (xs < b[1] - margin))[None, :]
        ok &= ((ys > b[2] + margin) & (ys < b[3] - margin))[:, None]
        self.ok = ok.ravel().tolist()
        self.nbr = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
                    (-1, -1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (1, 1, 1.4142)]
        self.job = None
        self.path = None
        self.goal = None

    def cell(self, x, y):
        i = int((x - self.w.minX) / CELL)
        j = int((y - self.w.minY) / CELL)
        return min(max(i, 0), self.W - 1), min(max(j, 0), self.H - 1)

    def center(self, k):
        j, i = divmod(k, self.W)
        return (self.w.minX + (i + 0.5) * CELL, self.w.minY + (j + 0.5) * CELL)

    def start(self, sx, sy, gx, gy, avoid=None):
        si, sj = self.cell(sx, sy)
        gi, gj = self.cell(gx, gy)
        s = sj * self.W + si
        g = gj * self.W + gi
        self.job = {"s": s, "g": g, "gi": gi, "gj": gj, "open": [(0.0, s)], "gs": {s: 0.0}, "par": {s: -1},
                    "closed": set(), "goal": (gx, gy), "best": s, "bestd": 1e18, "avoid": avoid}

    def step(self, budget_s: float):
        """Advance the current search; returns a path (list of (x,y)) when done, else None."""
        job = self.job
        if job is None:
            return None
        t_end = time.perf_counter() + budget_s
        W, Hh = self.W, self.H
        h = self.h
        ok = self.ok
        op = job["open"]
        gs = job["gs"]
        par = job["par"]
        closed = job["closed"]
        g = job["g"]
        gi, gj = job["gi"], job["gj"]
        su = self.step_up
        mc = self.max_climb
        avoid = job["avoid"]
        n = 0
        found = None
        while op:
            n += 1
            if (n & 63) == 0 and time.perf_counter() > t_end:
                return None
            f, k = heapq.heappop(op)
            if k in closed:
                continue
            closed.add(k)
            if k == g:
                found = k
                break
            j, i = divmod(k, W)
            dg = math.hypot(i - gi, j - gj)
            if dg < job["bestd"]:
                job["bestd"] = dg
                job["best"] = k
            gk = gs[k]
            hk = h[k]
            for di, dj, dc in self.nbr:
                ii = i + di
                jj = j + dj
                if ii < 0 or jj < 0 or ii >= W or jj >= Hh:
                    continue
                kk = jj * W + ii
                if kk in closed or not ok[kk]:
                    continue
                dh = h[kk] - hk
                c = dc
                if dh > su:
                    if dh > mc:
                        continue
                    c += 0.25 + dh * 0.45
                elif dh < -8:
                    c += 0.3
                if avoid is not None:
                    c += avoid(ii, jj)
                ng = gk + c
                if ng < gs.get(kk, 1e18):
                    gs[kk] = ng
                    par[kk] = k
                    heapq.heappush(op, (ng + math.hypot(ii - gi, jj - gj), kk))
            if len(closed) > 30000:
                break
        if found is None:
            found = job["best"]
        pts = []
        k = found
        while k != -1:
            pts.append(self.center(k))
            k = par[k]
        pts.reverse()
        self.job = None
        self.path = pts
        self.goal = job["goal"]
        if found == g:
            pts.append(job["goal"])
        return pts
