import glob, gzip, json, math, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from world import World
w = World(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'arena'))
def wrap(a): return (a + math.pi) % (2 * math.pi) - math.pi
for p in sys.argv[1:]:
    msgs = []
    try:
        for l in gzip.open(p, 'rt'): msgs.append(json.loads(l))
    except Exception: pass
    obs = [m for m in msgs if m['type'] == 'obs']
    acts = {m['tick']: m for m in msgs if m['type'] == 'act'}
    prev = None
    for k, o in enumerate(obs):
        lw = next((x for x in o['self']['weapons'] if x and x['kind'] == 'laser'), None)
        if lw is None: prev = None; continue
        ch = lw.get('charge', 0)
        if prev is not None and prev[1] > 0 and ch == 0:
            po = prev[0]
            hit = any(e['type'] == 'hit' and e['weapon'] == 'laser' for oo in obs[k:k + 3] for e in oo['events'])
            s = po['self']; a = s['aim']
            mz = [s['pos'][0], s['pos'][1], s['pos'][2] + s['stats'].get('muzzle_height', 7)]
            best = None
            for m in po['mechs']:
                tp = [m['pos'][0], m['pos'][1], m['pos'][2] + 4.5]
                d = math.dist(mz, tp)
                yaw = math.atan2(tp[1] - mz[1], tp[0] - mz[0]); pit = math.atan2(tp[2] - mz[2], math.hypot(tp[0] - mz[0], tp[1] - mz[1]))
                err = math.hypot(wrap(yaw - a[0]), pit - a[1]) * d
                if best is None or err < best[0]: best = (err, m['id'], round(d), w.los(mz, tp), m['kind'], [round(v, 1) for v in m['vel']])
            act = acts.get(po['tick'], {}).get('a', {})
            print(f"{os.path.basename(p)[:5]} t={po['t']:.1f} hit={hit} charge={prev[1]:.2f} fire_cmd={act.get('fire')} closest(err_m,id,d,los,kind,vel)={best} label={acts.get(po['tick'],{}).get('label')}")
        prev = (o, ch)
