"""Mech Royale bot: survival-first, laser-hunting, zone-aware."""
from __future__ import annotations

import math
import os
import random
import sys
import time

import mechfight as mf

try:
    from world import World
    from nav import Nav
except Exception:  # pragma: no cover
    World = None
    Nav = None

PI = math.pi
TAU = 2 * math.pi


def wrap(a: float) -> float:
    return (a + PI) % TAU - PI


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


WEAPON_RANK = {"laser": 3.0, "lightning": 2.0, "mortar": 1.5, "gun": 0.5}

UPGRADE_ORDER = [
    "plating", "armor", "nanites", "laser_damage", "laser_charge", "gun_damage", "gun_rate",
    "lightning_efficiency", "servos", "gun_mag", "lightning_range", "mortar_reload", "capacitor",
    "reactor", "sensors", "jets", "gyros", "gun_velocity", "lightning_chain", "mortar_velocity",
    "mortar_splash", "laser_pierce",
]


class Track:
    __slots__ = ("id", "pos", "vel", "aim", "hp", "max_hp", "weapons", "cubes", "charging", "seen_t", "first_t", "kind")

    def __init__(self, m, t):
        self.id = m["id"]
        self.first_t = t
        self.update(m, t)

    def update(self, m, t):
        self.kind = m.get("kind", "mech")
        self.pos = m["pos"]
        self.vel = m.get("vel", [0, 0, 0])
        self.aim = m.get("aim", [0, 0])
        self.hp = m.get("hp", 1400)
        self.max_hp = m.get("max_hp", 1400)
        self.weapons = m.get("weapons", ["gun"])
        self.cubes = m.get("cubes", 0)
        self.charging = m.get("charging", False)
        self.seen_t = t


class Bot:
    name = "ohio-steel"
    HEAL_BELOW = 0.8

    def __init__(self, hello):
        self.cfg = cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        self.rng = random.Random(hello.get("seed", 1))
        self.mech = cfg["mech"]
        self.W = cfg["weapons"]
        self.loot_r = cfg["loot"]["pickup_radius"]
        self.cube_r = cfg["cubes"]["pickup_radius"]
        self.world = None
        self.nav = None
        try:
            md = hello["map"]["dir"]
            self.world = World(md, hello["map"].get("files"))
            self.nav = Nav(self.world, step_up=self.mech.get("step_up", 1.2))
            mf.log(f"world built in {self.world.build_s:.2f}s")
        except Exception as e:  # keep playing without a map
            mf.log(f"world load failed: {e!r}")
        self.enemies: dict = {}
        self.npcs: dict = {}
        self.items: dict = {}  # id -> [cat, kind, pos, seen_t]
        self.threats = []  # (t, bearing, by, weapon, amount)
        self.sounds = []  # (t, bearing, dist, kind)
        self.last_dmg_t = -99.0
        self.last_hit_t = -99.0
        self.strafe = 1.0
        self.strafe_until = 0.0
        self.scan_yaw = 0.0
        self.wall_side = 1.0
        self.wall_side_t = -99.0
        self.stuck_t = 0.0
        self.last_pos = None
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.path_i = 0
        self.mortar_lock = 0.0
        self.mode = "start"
        self.cover = None
        self.cover_t = -99.0
        self.t = 0.0
        self.tick_ms = []
        self.blocked_targets = {}

    # ------------------------------------------------------------------ geometry helpers
    def los(self, a, b):
        if self.world is None:
            return True
        try:
            return self.world.los(a, b)
        except Exception:
            return True

    def surf(self, x, y):
        if self.world is None:
            return -1e9
        return self.world.surf_z(x, y)

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        self.t0 = t0
        self.t = t = obs["t"]
        me = obs["self"]
        self.me = me
        self.pos = me["pos"]
        self.stats = me["stats"]
        act: dict = {}

        self.update_memory(obs)

        # upgrade offer
        off = obs.get("offer")
        if off:
            act["pick"] = self.choose_upgrade(off)

        # weapons
        wk = [w["kind"] if w else None for w in me["weapons"]]
        self.wk = wk

        enemies = [e for e in self.enemies.values() if t - e.seen_t < 0.15]
        self.vis_enemies = enemies
        recent = [e for e in self.enemies.values() if t - e.seen_t < 6.0]

        # -------- loot interaction (take weapons)
        self.handle_take(obs, act)

        hpf = me["hp"] / max(1.0, me["max_hp"])
        self.healing = hpf < self.HEAL_BELOW and not enemies
        # -------- combat: choose target and aim/fire
        aim, fire, extra = self.combat(obs, enemies)

        # -------- movement brain
        move, boost, jump = self.brain(obs, enemies, recent)

        if extra.get("reload"):
            act["reload"] = True
        act["move"] = [round(move[0], 4), round(move[1], 4)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        act["aim"] = [round(aim[0], 4), round(clamp(aim[1], self.stats.get("min_pitch", -0.7), self.stats.get("max_pitch", 1.3)), 4)]
        act["fire"] = fire
        dt = (time.perf_counter() - t0) * 1000
        self.tick_ms.append(dt)
        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} e{me['energy']:.0f} {dt:.0f}ms")
        return act

    # ------------------------------------------------------------------ memory
    def update_memory(self, obs):
        t = self.t
        me = self.me
        for m in obs["mechs"]:
            if m["kind"] == "mech":
                tr = self.enemies.get(m["id"])
                if tr is None:
                    self.enemies[m["id"]] = Track(m, t)
                else:
                    tr.update(m, t)
            else:
                tr = self.npcs.get(m["id"])
                if tr is None:
                    self.npcs[m["id"]] = Track(m, t)
                else:
                    tr.update(m, t)
        # forget long-stale
        for d in (self.enemies, self.npcs):
            for k in [k for k, v in d.items() if t - v.seen_t > 30]:
                del d[k]
        listed = set()
        for cat in ("pickups", "crates", "cubes"):
            for it in obs.get(cat, []):
                listed.add(it["id"])
                self.items[it["id"]] = [cat, it.get("kind", cat), it["pos"], t, it.get("hp", 0)]
        px, py, pz = self.pos
        eye = [px, py, pz + self.stats.get("eye_height", 8)]
        near = 45 + self.stats.get("near_range_bonus", 0)
        for k in list(self.items.keys()):
            cat, kind, p, st, hp = self.items[k]
            if k in listed:
                continue
            d = hyp(p[0] - px, p[1] - py)
            if t - st > 90 or (d < near - 8 and abs(p[2] - pz) < 20 and self.los(eye, [p[0], p[1], p[2] + 1.0])):
                del self.items[k]
        for e in obs["events"]:
            ty = e.get("type")
            if ty == "damaged":
                self.last_dmg_t = t
                if e.get("bearing") is not None:
                    self.threats.append((t, e["bearing"], e.get("by"), e.get("weapon"), e.get("amount", 0)))
            elif ty == "hit":
                self.last_hit_t = t
            elif ty == "kill":
                mf.log(f"t={t:.1f} killed {e.get('target')}")
            elif ty == "took":
                mf.log(f"t={t:.1f} took {e.get('kind')} slot {e.get('slot')}")
            elif ty == "take_failed":
                self.items.pop(e.get("id"), None)
        self.threats = [x for x in self.threats if t - x[0] < 6]
        for s in obs.get("sounds", []):
            self.sounds.append((t - s.get("age", 0), s["bearing"], s["dist"], s["kind"]))
        self.sounds = [s for s in self.sounds if t - s[0] < 5]

    # ------------------------------------------------------------------ upgrades
    def choose_upgrade(self, off):
        best, bi = 1e9, 0
        for i, c in enumerate(off["choices"]):
            try:
                r = UPGRADE_ORDER.index(c["id"])
            except ValueError:
                r = 50
            if c["id"].startswith("laser") and "laser" not in self.me_kinds():
                r += 30
            if c["id"].startswith("lightning") and "lightning" not in self.me_kinds():
                r += 30
            if c["id"].startswith("mortar") and "mortar" not in self.me_kinds():
                r += 30
            if r < best:
                best, bi = r, i
        return bi

    def me_kinds(self):
        return [w["kind"] for w in self.me["weapons"] if w]

    # ------------------------------------------------------------------ loot
    def want_weapon(self, kind):
        """Value of taking this weapon kind now (0 = don't), and slot to drop."""
        wk = self.wk
        if kind in wk:
            return 0.0, None
        if kind == "gun":
            return 0.0, None
        rank = WEAPON_RANK.get(kind, 1)
        if wk[1] is None:
            if wk[0] is None:
                return rank + 1, None
            return rank, None
        # both full: swap the worse non-gun if this is better
        cur = wk[1]
        if WEAPON_RANK.get(cur, 0) + 0.4 < rank:
            # can't drop a charging laser
            w = self.me["weapons"][1]
            if w and w.get("charge", 0) > 0:
                return 0.0, None
            return rank - WEAPON_RANK.get(cur, 0), 1
        return 0.0, None

    def handle_take(self, obs, act):
        px, py, pz = self.pos
        best = None
        for p in obs.get("pickups", []):
            d = hyp(p["pos"][0] - px, p["pos"][1] - py)
            if d <= self.loot_r - 0.3 and abs(p["pos"][2] - pz) < 3.8:
                v, drop = self.want_weapon(p["kind"])
                if v > 0 and (best is None or v > best[0]):
                    best = (v, p["id"], drop)
        if best:
            act["take"] = best[1]
            if best[2] is not None:
                act["drop"] = best[2]

    # ------------------------------------------------------------------ combat
    def target_point(self, e):
        sc = 1 + 0.05 * (e.cubes or 0)
        if e.kind == "npc":
            return [e.pos[0], e.pos[1], e.pos[2] + 1.75]
        return [e.pos[0], e.pos[1], e.pos[2] + 4.5 * sc]

    def muzzle(self):
        px, py, pz = self.pos
        return [px, py, pz + self.stats.get("muzzle_height", 7)]

    def lead(self, src, tgt, vel, speed, grav=False, ground=None):
        tx, ty, tz = tgt
        vx, vy, vz = vel
        tt = 0.0
        for _ in range(3):
            d = math.sqrt((tx + vx * tt - src[0]) ** 2 + (ty + vy * tt - src[1]) ** 2 + (tz + vz * tt - src[2]) ** 2)
            tt = d / speed + 0.05
        ex, ey, ez = tx + vx * tt, ty + vy * tt, tz + vz * tt
        if grav and abs(vz) > 0.5:
            ez = tz + vz * tt - 0.5 * self.mech.get("gravity", 22) * tt * tt
            if ground is not None:
                ez = max(ez, ground)
        return [ex, ey, ez], tt

    def aim_at(self, src, p):
        dx, dy, dz = p[0] - src[0], p[1] - src[1], p[2] - src[2]
        return [math.atan2(dy, dx), math.atan2(dz, hyp(dx, dy))]

    def aim_err(self, want):
        a = self.me["aim"]
        return abs(wrap(want[0] - a[0])), abs(want[1] - a[1])

    def mortar_solve(self, src, tgt, vel):
        v = self.wstats("mortar").get("speed", 72)
        g = self.wstats("mortar").get("gravity", 22)
        p = list(tgt)
        best = None
        for _ in range(3):
            dx, dy = p[0] - src[0], p[1] - src[1]
            d = hyp(dx, dy)
            dz = p[2] - src[2]
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1:
                return None
            sq = math.sqrt(disc)
            sols = []
            for sgn in (-1, 1):
                th = math.atan((v * v + sgn * sq) / (g * d))
                ft = d / (v * math.cos(th))
                sols.append((th, ft))
            best = sols
            # lead using the low arc flight time
            ft = sols[0][1]
            p = [tgt[0] + vel[0] * ft, tgt[1] + vel[1] * ft, tgt[2]]
        yaw = math.atan2(p[1] - src[1], p[0] - src[0])
        maxp = self.stats.get("max_pitch", 1.3)
        for th, ft in best:
            if th > maxp or th < self.stats.get("min_pitch", -0.7):
                continue
            if self.arc_clear(src, yaw, th, v, g, ft, p):
                return yaw, th, ft
        return None

    def arc_clear(self, src, yaw, th, v, g, ft, p):
        if self.world is None:
            return True
        cx, sx = math.cos(yaw), math.sin(yaw)
        vh, vz = v * math.cos(th), v * math.sin(th)
        n = 14
        for k in range(1, n):
            tt = ft * k / n
            x = src[0] + cx * vh * tt
            y = src[1] + sx * vh * tt
            z = src[2] + vz * tt - 0.5 * g * tt * tt
            if hyp(x - p[0], y - p[1]) < 9:
                return True
            if z < self.world.top_z(x, y) - 0.2:
                return False
        return True

    def wstats(self, kind):
        for w in self.me["weapons"]:
            if w and w["kind"] == kind:
                return w["stats"]
        return self.W.get(kind, {})

    def pick_target(self, enemies):
        if not enemies:
            return None
        mz = self.muzzle()
        best, bs = None, 1e18
        for e in enemies:
            tp = self.target_point(e)
            d = math.dist(mz, tp)
            vis = self.los(mz, tp) or self.los(mz, [tp[0], tp[1], tp[2] + 3])
            s = d + e.hp / 6.0 + (0 if vis else 150)
            if e.charging:
                s -= 40
                if self.aimed_at_me(e):
                    s -= 150
            if "laser" in (e.weapons or []):
                s -= 25
            if s < bs:
                bs, best = s, e
        return best

    def combat(self, obs, enemies):
        me = self.me
        t = self.t
        fire = [False, False]
        extra = {}
        weapons = me["weapons"]
        mz = self.muzzle()
        tgt = self.pick_target(enemies)
        self.target = tgt
        farm = None
        if tgt is None:
            farm = self.farm_target(obs)
        self.farm = farm
        laser_slot = next((i for i, w in enumerate(weapons) if w and w["kind"] == "laser"), None)
        charging = laser_slot is not None and weapons[laser_slot].get("charge", 0) > 0

        if tgt is None and farm is None:
            # no target: scan; release laser charge holding pointlessly is fine (auto-fires)
            aim = self.scan_aim(obs)
            if charging:
                fire[laser_slot] = True  # keep holding; auto-fires eventually
            g = weapons[0] if weapons[0] and weapons[0]["kind"] == "gun" else None
            if g and g.get("reload", 0) == 0 and g.get("ammo", 99) < g["stats"].get("mag", 12) * 0.6:
                extra["reload"] = True
            return aim, fire, extra

        if tgt is None:
            tp, tvel, kind_t, tgt_obj = farm
        else:
            tp, tvel, kind_t, tgt_obj = self.target_point(tgt), tgt.vel, "mech", tgt
        d = math.dist(mz, tp)
        visible = self.los(mz, tp)
        if not visible and kind_t == "mech":
            # aim at whatever part of the body is exposed (over a wall: upper torso; under an overhang: legs)
            sc = 1 + 0.05 * (getattr(tgt_obj, "cubes", 0) or 0)
            for dz in (2.8 * sc, 3.8 * sc, -2.8 * sc, 1.5 * sc):
                cand = [tp[0], tp[1], tp[2] + dz]
                if self.los(mz, cand):
                    tp = cand
                    visible = True
                    break
        ground = None
        if self.world is not None:
            ground = self.world.surface_below(tp[0], tp[1], tp[2] - 4) + 0.5

        # gun solution
        gs = self.wstats("gun")
        gp, _ = self.lead(mz, tp, tvel, gs.get("speed", 252), grav=True, ground=ground)
        gun_aim = self.aim_at(mz, gp)
        aim = gun_aim
        slot_of = {w["kind"]: i for i, w in enumerate(weapons) if w}

        # mortar
        if "mortar" in slot_of and kind_t == "mech":
            mw = weapons[slot_of["mortar"]]
            if mw["cooldown"] <= 0.15 and 25 < d < 230:
                src = [self.pos[0], self.pos[1], self.pos[2] + self.stats.get("height", 9) + 0.5]
                sol = self.mortar_solve(src, [tp[0], tp[1], tp[2] - 3.5], tvel)
                if sol is not None and (visible or sol[2] < 4.5):
                    aim = [sol[0], sol[1]]
                    ey, ep = self.aim_err(aim)
                    if ey < 0.025 and ep < 0.02 and mw["cooldown"] <= 0:
                        fire[slot_of["mortar"]] = True

        # laser
        if laser_slot is not None:
            lw = weapons[laser_slot]
            ls = lw["stats"]
            full = ls.get("charge", 0.75)
            ch = lw.get("charge", 0)
            in_range = d < ls.get("range", 280) - 5
            if ch > 0:
                if ch >= full - 0.12 and visible and in_range:
                    lp = [tp[0] + tvel[0] * 0.08, tp[1] + tvel[1] * 0.08, tp[2] + tvel[2] * 0.05]
                    aim = self.aim_at(mz, lp)
                    ey, ep = self.aim_err(aim)
                    tol = math.atan2(1.6, max(d, 1))
                    if ch >= full - 0.02 and ey < tol + 0.006 and ep < tol + 0.006:
                        fire[laser_slot] = False  # release
                    else:
                        fire[laser_slot] = True
                else:
                    fire[laser_slot] = True  # keep charging
            else:
                if lw["cooldown"] <= 0.05 and in_range and visible and kind_t in ("mech", "npc"):
                    fire[laser_slot] = True  # start charge

        # lightning
        if "lightning" in slot_of:
            lw = weapons[slot_of["lightning"]]
            ls = lw["stats"]
            if d < ls.get("range", 45) * 0.97 and visible and me["energy"] > ls.get("energy", 3) + 2:
                ey, _ = self.aim_err([math.atan2(tp[1] - mz[1], tp[0] - mz[0]), 0])
                if ey < math.radians(ls.get("cone_deg", 30)) * 0.8:
                    fire[slot_of["lightning"]] = True

        # gun
        if "gun" in slot_of:
            gw = weapons[slot_of["gun"]]
            ey, ep = self.aim_err(gun_aim)
            tol = math.atan2(2.0 if kind_t == "mech" else 1.2, max(d, 1)) + 0.01
            if visible and d < gs.get("range", 230) and gw.get("reload", 0) == 0 and gw.get("ammo", 1) > 0:
                if ey < tol + 0.12 and ep < tol + 0.08 and aim is gun_aim:
                    fire[slot_of["gun"]] = True
                elif ey < tol and ep < tol:
                    fire[slot_of["gun"]] = True
            if gw.get("ammo", 1) == 0 and gw.get("reload", 0) == 0:
                extra["reload"] = True
        return aim, fire, extra

    def farm_target(self, obs):
        """NPCs and crates to shoot when no enemy mech is visible."""
        t = self.t
        if self.healing:
            return None
        if t - max((e.seen_t for e in self.enemies.values()), default=-99) < 3.0:
            return None
        mz = self.muzzle()
        best, bs = None, 1e18
        for n in self.npcs.values():
            if t - n.seen_t > 0.15:
                continue
            tp = [n.pos[0], n.pos[1], n.pos[2] + 1.75]
            d = math.dist(mz, tp)
            if d > 180 or not self.los(mz, tp):
                continue
            s = d
            if s < bs:
                bs, best = s, (tp, n.vel, "npc", n)
        for c in obs.get("crates", []):
            tp = [c["pos"][0], c["pos"][1], c["pos"][2] + 1.2]
            d = math.dist(mz, tp)
            if d > 150 or not self.los(mz, tp):
                continue
            s = d + 30
            if s < bs:
                bs, best = s, (tp, [0, 0, 0], "crate", c)
        return best

    def scan_aim(self, obs):
        t = self.t
        # turn toward recent damage
        if self.threats:
            tt, b, by, wpn, amt = self.threats[-1]
            if t - tt < 2.5:
                return [b, 0.0]
        # loud recent sounds
        loud = [s for s in self.sounds if s[3] in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet") and t - s[0] < 1.5]
        if loud:
            s = loud[-1]
            return [s[1], 0.0]
        # recently seen enemy
        rec = sorted(self.enemies.values(), key=lambda e: -e.seen_t)
        if rec and t - rec[0].seen_t < 8:
            e = rec[0]
            return [math.atan2(e.pos[1] - self.pos[1], e.pos[0] - self.pos[0]), 0.0]
        self.scan_yaw = wrap(self.scan_yaw + 0.35)
        return [self.scan_yaw, 0.0]

    # ------------------------------------------------------------------ zone
    def zone_target(self, obs):
        """(safe_center, safe_radius, urgency) for where we must be."""
        z = obs["zone"]
        t = self.t
        c, r = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        ss, se = z["shrink_start"], z["shrink_end"]
        px, py = self.pos[0], self.pos[1]
        speed = self.stats.get("walk_speed", 11.7)
        dn = hyp(px - nc[0], py - nc[1])
        need = max(0.0, dn - nr * 0.75)
        travel = need / (speed * 0.8) + 8.0
        if t >= ss - travel or t >= ss:
            return nc, nr, (t - (ss - travel))
        # live circle (assume obs center/radius already live)
        return c, r, -1.0

    # ------------------------------------------------------------------ brain
    def brain(self, obs, enemies, recent):
        me = self.me
        t = self.t
        px, py, pz = self.pos
        hpf = me["hp"] / max(1.0, me["max_hp"])
        zc, zr, urg = self.zone_target(obs)
        dz = hyp(px - zc[0], py - zc[1])
        z = obs["zone"]
        cur_c, cur_r = z["center"], z["radius"]
        out_now = not me.get("in_zone", True)
        boost = False
        speed = 1.0
        dest = None
        face_move = False

        # zone-forced
        margin = min(25.0, zr * 0.25)
        zone_bad = dz > zr - margin
        if out_now or (zone_bad and urg > 0):
            self.mode = "rotate"
            inner = max(0.0, zr * 0.55)
            if dz > 1e-6:
                dest = (zc[0] + (px - zc[0]) / dz * inner, zc[1] + (py - zc[1]) / dz * inner)
            else:
                dest = (zc[0], zc[1])
            boost = out_now or urg > 3 or dz > zr
        threat_n = len([e for e in enemies if math.dist(e.pos, self.pos) < 220])
        tgt = self.target
        if dest is None and enemies:
            duck = self.laser_duck(enemies, zc, zr)
            if duck is not None:
                self.mode = "duck"
                move, b2, jump = self.steer(duck, 1.0, obs, direct=True)
                return move, me["energy"] > 15, jump
            strong = sum(e.hp for e in enemies if math.dist(e.pos, self.pos) < 200)
            lone = threat_n <= 1
            if tgt is not None and ((lone and (hpf > 0.6 or me["hp"] >= 0.55 * tgt.hp)) or (threat_n == 2 and me["hp"] > strong * 0.8)):
                self.mode = "fight"
                return self.fight_move(tgt, zc, zr)
            self.mode = "evade"
            dest = self.cover_point(enemies, zc, zr)
            boost = me["energy"] > 25
        if dest is None and self.threats and t - self.threats[-1][0] < 3 and not enemies:
            # shot by something unseen: get to cover from that bearing
            self.mode = "cover"
            b = self.threats[-1][1]
            fake = [px + math.cos(b) * 100, py + math.sin(b) * 100, pz]
            dest = self.cover_point_from([fake], zc, zr)
            boost = me["energy"] > 40
        if dest is None and self.healing:
            dest = self.heal_spot(obs, zc, zr)
            if dest is not None:
                self.mode = "heal"
                speed = 1.0 if hyp(dest[0] - px, dest[1] - py) > 4 else 0.0
        if dest is None:
            dest, why = self.loot_or_hold(obs, zc, zr)
            self.mode = why
            if why == "hold":
                speed = 0.38 if hyp(dest[0] - px, dest[1] - py) < 30 else 1.0
        move, b2, jump = self.steer(dest, speed, obs)
        boost = boost or b2
        # dodge incoming projectiles
        dm = self.dodge(obs)
        if dm is not None:
            move = self.norm2(move[0] * 0.5 + dm[0], move[1] * 0.5 + dm[1])
        return move, boost, jump

    def norm2(self, x, y):
        n = hyp(x, y)
        if n < 1e-9:
            return (0.0, 0.0)
        return (x / n, y / n)

    def aimed_at_me(self, e, tol=0.16):
        px, py, pz = self.pos
        b = math.atan2(py - e.pos[1], px - e.pos[0])
        return abs(wrap(b - e.aim[0])) < tol

    def laser_duck(self, enemies, zc, zr):
        """An enemy is charging a laser at us and we can't answer: step behind cover if it's close."""
        th = [e for e in enemies if e.charging and self.aimed_at_me(e)]
        if not th:
            return None
        lw = next((w for w in self.me["weapons"] if w and w["kind"] == "laser"), None)
        if lw is not None and (lw.get("charge", 0) >= 0.35 or lw["cooldown"] < 0.2):
            return None  # duel it
        srcs = [[e.pos[0], e.pos[1], e.pos[2] + 8] for e in th]
        self.cover_t = -99
        c = self.cover_point_from(srcs, zc, zr)
        px, py, pz = self.pos
        if c is None or hyp(c[0] - px, c[1] - py) > 20:
            return None
        gz = self.surf(c[0], c[1])
        if all(not self.los(sp, [c[0], c[1], gz + 5]) for sp in srcs):
            return c
        return None

    def fight_move(self, tgt, zc, zr):
        me = self.me
        t = self.t
        px, py, pz = self.pos
        ex, ey = tgt.pos[0], tgt.pos[1]
        d = hyp(ex - px, ey - py)
        kinds = self.wk
        tw = tgt.weapons or []
        if "lightning" in kinds and "mortar" not in tw:
            want = 26.0
        elif "laser" in kinds:
            want = 110.0 if "lightning" in tw else 90.0
        elif "mortar" in kinds:
            want = 90.0
        else:
            want = 60.0
        if "lightning" in tw and "lightning" not in kinds:
            want = max(want, 70.0)
        if t > self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < 0.7 else self.strafe
            self.strafe_until = t + self.rng.uniform(0.35, 1.1)
        # react to their laser charge: jink now
        if tgt.charging and t > self.strafe_until - 0.25:
            self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(0.3, 0.6)
        ux, uy = (ex - px) / max(d, 1e-6), (ey - py) / max(d, 1e-6)
        kite = "lightning" in tw and "lightning" not in kinds and d < 60
        radial = clamp((d - want) / (12.0 if kite else 25.0), -1.0, 1.0)
        tx, ty = -uy * self.strafe, ux * self.strafe
        tw_ = 0.9 * (1 - 0.6 * abs(radial)) if kite else 0.9
        mx, my = ux * radial + tx * tw_, uy * radial + ty * tw_
        # keep inside the safe circle
        dzc = hyp(px - zc[0], py - zc[1])
        if dzc > zr * 0.8:
            mx += (zc[0] - px) / max(dzc, 1) * 1.2
            my += (zc[1] - py) / max(dzc, 1) * 1.2
        mv = self.norm2(mx, my)
        dest = (px + mv[0] * 12, py + mv[1] * 12)
        move, boost, jump = self.steer(dest, 1.0, None, direct=True)
        jump = jump or (tgt.charging and me["grounded"] and self.rng.random() < 0.3)
        boost = boost or (me["energy"] > 60 and d < want - 20) or (kite and me["energy"] > 20)
        dm = self.dodge(None)
        if dm is not None:
            move = self.norm2(move[0] * 0.5 + dm[0], move[1] * 0.5 + dm[1])
        return move, boost, jump

    def dodge(self, obs):
        o = self.obs_cache
        if o is None:
            return None
        px, py, pz = self.pos
        cz = pz + 4.5
        best = None
        for p in o.get("projectiles", []):
            vx, vy, vz = p["vel"]
            x, y, z = p["pos"]
            if p["kind"] == "mortar":
                g = self.W["mortar"].get("gravity", 22)
                # impact time where z hits our ground level
                a, b_, c = -0.5 * g, vz, z - pz
                disc = b_ * b_ - 4 * a * c
                if disc < 0:
                    continue
                ti = (-b_ - math.sqrt(disc)) / (2 * a)
                if ti < 0 or ti > 7:
                    continue
                ix, iy = x + vx * ti, y + vy * ti
                mvx, mvy = self.me["vel"][0], self.me["vel"][1]
                fx, fy = px + mvx * ti, py + mvy * ti
                dd = hyp(ix - fx, iy - fy)
                R = self.W["mortar"].get("splash_radius", 7) + 4
                if dd < R:
                    dx, dy = fx - ix, fy - iy
                    if dd < 0.5:
                        dx, dy = -vy, vx
                    best = self.norm2(dx, dy)
            else:
                sp2 = vx * vx + vy * vy + vz * vz
                if sp2 < 1:
                    continue
                rx, ry, rz = px - x, py - y, cz - z
                tc = (rx * vx + ry * vy + rz * vz) / sp2
                if tc < 0 or tc > 0.8:
                    continue
                cx, cy, ccz = x + vx * tc - px, y + vy * tc - py, z + vz * tc - cz
                miss = math.sqrt(cx * cx + cy * cy)
                if miss < 3.2 and abs(ccz) < 5:
                    # move perpendicular to the bullet, away from its closest point
                    nx, ny = -vy, vx
                    if nx * cx + ny * cy > 0:
                        nx, ny = -nx, -ny
                    best = self.norm2(nx, ny)
        return best

    def cover_point(self, enemies, zc, zr):
        srcs = [[e.pos[0], e.pos[1], e.pos[2] + 8] for e in enemies]
        return self.cover_point_from(srcs, zc, zr)

    def cover_point_from(self, srcs, zc, zr):
        t = self.t
        px, py, pz = self.pos
        if self.cover is not None and t - self.cover_t < 0.6:
            return self.cover
        if time.perf_counter() - self.t0 > 0.014:
            return self.cover or (px, py)
        best, bs = None, -1e18
        cx = sum(s[0] for s in srcs) / len(srcs)
        cy = sum(s[1] for s in srcs) / len(srcs)
        away = math.atan2(py - cy, px - cx)
        for dist in (14.0, 28.0, 45.0):
            for k in range(12):
                a = away + (k - 5.5) * (TAU / 12)
                x, y = px + math.cos(a) * dist, py + math.sin(a) * dist
                if self.world is not None and not self.world.inside_bounds(x, y, 8):
                    continue
                gz = self.surf(x, y)
                if gz - pz > 6:
                    continue  # don't target roofs/walls
                s = 0.0
                hidden = 0
                for sp in srcs:
                    if not self.los(sp, [x, y, gz + 5]):
                        hidden += 1
                    s += min(hyp(x - sp[0], y - sp[1]), 200) * 0.05
                s += hidden * 30 / len(srcs)
                dzc = hyp(x - zc[0], y - zc[1])
                if dzc > zr * 0.85:
                    s -= (dzc - zr * 0.85) * 2
                s -= dist * 0.15
                # path blocked by tall wall in between?
                if self.world is not None:
                    hm = max(self.surf(px + (x - px) * f, py + (y - py) * f) for f in (0.3, 0.6, 0.9))
                    if hm - pz > 3:
                        s -= 12
                if s > bs:
                    bs, best = s, (x, y)
            if time.perf_counter() - self.t0 > 0.016:
                break
        self.cover = best or (px, py)
        self.cover_t = t
        return self.cover

    def heal_spot(self, obs, zc, zr):
        """Stand still out of sight of recently seen enemies (and threat bearings) while HP regenerates."""
        t = self.t
        px, py, pz = self.pos
        # grab cubes that are very close first
        for k, (cat, kind, p, st, hp) in self.items.items():
            if cat == "cubes" and hyp(p[0] - px, p[1] - py) < 25 and abs(p[2] - pz) < 6:
                return None
        srcs = [[e.pos[0], e.pos[1], e.pos[2] + 8] for e in self.enemies.values() if t - e.seen_t < 20]
        for tt, b, by, wpn, amt in self.threats:
            if t - tt < 6 and by is not None and by not in self.enemies:
                srcs.append([px + math.cos(b) * 80, py + math.sin(b) * 80, pz + 8])
        if not srcs:
            return (px, py)
        eye = [px, py, pz + 6]
        if all(not self.los(sp, eye) for sp in srcs):
            return (px, py)  # already hidden: stay still and silent
        if self.cover is not None and t - self.cover_t < 1.5:
            return self.cover
        self.cover_t = -99
        return self.cover_point_from(srcs, zc, zr)

    def loot_or_hold(self, obs, zc, zr):
        t = self.t
        px, py, pz = self.pos
        best, bs, why = None, -1e18, "hold"

        def in_safe(p, m=0.85):
            return hyp(p[0] - zc[0], p[1] - zc[1]) < zr * m

        for k, (cat, kind, p, st, hp) in self.items.items():
            d = hyp(p[0] - px, p[1] - py)
            if not in_safe(p):
                continue
            climb = max(0.0, p[2] - pz)
            if climb > 40:
                continue
            if cat == "pickups":
                v, _ = self.want_weapon(kind)
                if v <= 0:
                    continue
                s = 60 * v - d - climb * 1.5
                tag = "loot:" + kind
            elif cat == "cubes":
                s = 70 - d - climb * 1.5
                tag = "cube"
            else:
                continue
            if d > 220:
                continue
            if s > bs:
                bs, best, why = s, p, tag
        # supply drops
        for dr in obs.get("drops", []):
            p = dr["pos"]
            d = hyp(p[0] - px, p[1] - py)
            if not in_safe(p, 0.95):
                continue
            seen_recent = any(t - e.seen_t < 8 for e in self.enemies.values())
            if seen_recent:
                continue
            if dr["lands_in"] > d / 11.0 + 6:
                continue
            if dr.get("holder") not in (None, self.me["id"]):
                continue
            s = 90 - d * 0.5
            if s > bs:
                bs, best, why = s, (p[0], p[1], p[2]), "drop"
        if best is not None and bs > -60:
            return (best[0], best[1]), why
        # explore for a better weapon early on
        z = obs["zone"]
        if self.wk[1] != "laser" and t < 200 and z["next_radius"] > 60:
            ex = getattr(self, "explore", None)
            if ex is None or hyp(ex[0] - px, ex[1] - py) < 12 or t > self.explore_until:
                ex = None
                for _ in range(12):
                    a = self.rng.uniform(-PI, PI)
                    r = self.rng.uniform(70, 150)
                    cx, cy = px + math.cos(a) * r, py + math.sin(a) * r
                    nc = z["next_center"]
                    if hyp(cx - nc[0], cy - nc[1]) < z["next_radius"] * 0.8 and (self.world is None or self.world.inside_bounds(cx, cy, 20)):
                        ex = (cx, cy)
                        break
                if ex is None:
                    nc = z["next_center"]
                    ex = (nc[0], nc[1])
                self.explore = ex
                self.explore_until = t + 25
            return ex, "explore"
        # hold position: inside the *next* circle, toward its center
        zc, zr = z["next_center"], z["next_radius"]
        dzc = hyp(px - zc[0], py - zc[1])
        hold_r = zr * 0.45
        if dzc > hold_r and dzc > 1:
            return (zc[0] + (px - zc[0]) / dzc * hold_r, zc[1] + (py - zc[1]) / dzc * hold_r), "hold"
        return (px, py), "hold"

    # ------------------------------------------------------------------ steering
    def steer(self, dest, speed, obs, direct=False):
        me = self.me
        t = self.t
        px, py, pz = self.pos
        dx, dy = dest[0] - px, dest[1] - py
        d = hyp(dx, dy)
        jump = False
        boost = False
        if d < 1.2:
            return (0.0, 0.0), False, False
        # stuck detection
        hs = hyp(me["vel"][0], me["vel"][1])
        if hs < 1.5 and d > 3 and me["grounded"]:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.2)
        wx, wy = dest
        if not direct and self.nav is not None and d > 35:
            wx, wy = self.path_waypoint(dest)
        ux, uy = self.norm2(wx - px, wy - py)
        if self.world is None:
            return (ux * speed, uy * speed), False, False
        # obstacle ahead?
        su = self.stats.get("step_up", 1.2)
        energy = me["energy"]
        rr = self.stats.get("radius", 2.5)

        def ahead_h(ax, ay):
            return max(self.world.surface_below(px + ax * r, py + ay * r, pz) for r in (rr + 0.6, rr + 1.6, rr + 3.0))

        h = ahead_h(ux, uy)
        dh = h - pz
        if dh > su + 0.15:
            climb_cost = dh * 1.6 + 6
            # landing height beyond the wall: only climb onto something we can reach
            if dh < 50 and energy > climb_cost and (not direct or dh < 12):
                if me["grounded"]:
                    jump = True
                boost = True
                return (ux, uy), boost, jump
            # deflect along the wall
            if t - self.wall_side_t > 3.0:
                self.wall_side = 1.0 if self.rng.random() < 0.5 else -1.0
            self.wall_side_t = t
            base = math.atan2(uy, ux)
            for off in (0.45, 0.9, 1.3, 1.75, 2.2, 2.7):
                for sgn in (self.wall_side, -self.wall_side):
                    a = base + sgn * off
                    vx, vy = math.cos(a), math.sin(a)
                    if ahead_h(vx, vy) - pz <= su + 0.15:
                        self.wall_side = sgn
                        return (vx * speed, vy * speed), False, False
            return (ux, uy), energy > 30, me["grounded"]
        if not me["grounded"] and me["vel"][2] < 2 and pz < h - 0.3:
            boost = True
        if self.stuck_t > 0.8:
            self.stuck_t = 0.0
            self.path_t = -99
            return (ux, uy), energy > 25, True
        return (ux * speed, uy * speed), boost, jump

    def path_waypoint(self, dest):
        t = self.t
        px, py = self.pos[0], self.pos[1]
        nav = self.nav
        need = (self.path is None or self.path_goal is None or hyp(self.path_goal[0] - dest[0], self.path_goal[1] - dest[1]) > 20
                or t - self.path_t > 6)
        if need and nav.job is None:
            nav.start(px, py, dest[0], dest[1])
            self.path_t = t
            self.pending_goal = dest
        if nav.job is not None:
            left = 0.022 - (time.perf_counter() - self.t0)
            if left > 0.004:
                p = nav.step(min(0.008, left - 0.002))
                if p is not None:
                    self.path = p
                    self.path_goal = self.pending_goal
                    self.path_i = 0
        if self.path:
            pts = self.path
            # advance along path
            i = self.path_i
            bi, bd = i, 1e18
            for k in range(i, min(len(pts), i + 12)):
                dd = hyp(pts[k][0] - px, pts[k][1] - py)
                if dd < bd:
                    bd, bi = dd, k
            if bd > 25:
                self.path_t = -99  # off path: replan
            self.path_i = bi
            k = bi
            while k < len(pts) - 1 and hyp(pts[k][0] - px, pts[k][1] - py) < 7:
                k += 1
            return pts[k]
        return dest


class Runner(Bot):
    def act(self, obs):
        self.obs_cache = obs
        return super().act(obs)

    def __init__(self, hello):
        self.obs_cache = None
        super().__init__(hello)


if __name__ == "__main__":
    mf.run(Runner)
