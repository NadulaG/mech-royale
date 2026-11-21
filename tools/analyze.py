"""Summarise a recorded match (relay .jsonl.gz): hp timeline, damage taken/dealt, weapons, place."""
import gzip
import json
import math
import sys
from collections import Counter, defaultdict

for path in sys.argv[1:]:
    hello = None
    obs = []
    end = None
    results = None
    for line in gzip.open(path, "rt"):
        m = json.loads(line)
        if m["type"] == "hello":
            hello = m
        elif m["type"] == "obs":
            obs.append(m)
        elif m["type"] == "end":
            end = m
        elif m["type"] == "results":
            results = m
    print("==", path, "slot", hello and hello["slot"], "obs", len(obs), "end", end)
    taken = Counter()
    dealt = Counter()
    kills = []
    last_w = None
    for o in obs:
        s = o["self"]
        w = [x and x["kind"] for x in s["weapons"]]
        if w != last_w:
            print(f"  t={o['t']:.1f} weapons {w}")
            last_w = w
        for e in o["events"]:
            if e["type"] == "damaged":
                taken[(e.get("by"), e["weapon"])] += e["amount"]
            elif e["type"] == "hit":
                dealt[(e["target"], e["weapon"])] += e["amount"]
            elif e["type"] == "kill":
                kills.append((o["t"], e["target"]))
            elif e["type"] in ("upgrade", "drop", "pickup"):
                print(f"  t={o['t']:.1f} {e}")
        if o["tick"] % 100 == 0:
            vis = [(m["id"], round(math.dist(m["pos"][:2], s["pos"][:2])), m.get("weapons"), round(m.get("hp", 0))) for m in o["mechs"] if m["kind"] == "mech"]
            z = o["zone"]
            dz = math.dist(s["pos"][:2], z["center"]) - z["radius"]
            print(f"  t={o['t']:5.1f} alive={o['alive']:2d} hp={s['hp']:6.0f}/{s['max_hp']:.0f} e={s['energy']:3.0f} cubes={s['cubes']} pos={[round(v) for v in s['pos']]} zone_edge={dz:+.0f} seen={vis}")
    print("  damage taken:", dict(taken.most_common(10)), "total", sum(taken.values()))
    print("  damage dealt:", dict(dealt.most_common(10)), "total", sum(dealt.values()))
    print("  kills:", kills)
    if results:
        print("  results:", json.dumps(results)[:800])
