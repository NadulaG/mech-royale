"""Shots fired vs hits from relay recordings (crate hits are counted separately)."""
import glob, gzip, json, sys
from collections import Counter
tot = Counter()
for p in sorted(sys.argv[1:] or glob.glob('tools/runs/*.jsonl.gz')):
    obs = []
    try:
        for l in gzip.open(p, 'rt'):
            m = json.loads(l)
            if m['type'] == 'obs': obs.append(m)
    except Exception: pass
    prev_charge = 0; prev_ammo = None; prev_mcd = 0
    c = Counter()
    for o in obs:
        s = o['self']
        for e in o['events']:
            if e['type'] == 'hit':
                k = 'mech' if e['target'] < 100 else ('npc' if e['target'] < 1000 else 'crate')
                c[f'hit_{k}_{e["weapon"]}'] += 1
        for w in s['weapons']:
            if not w: continue
            if w['kind'] == 'laser':
                ch = w.get('charge', 0)
                if prev_charge > 0 and ch == 0: c['fired_laser'] += 1
                prev_charge = ch
            if w['kind'] == 'gun':
                a = w.get('ammo')
                if prev_ammo is not None and a is not None and a < prev_ammo: c['fired_gun'] += prev_ammo - a
                prev_ammo = a
            if w['kind'] == 'mortar':
                cd = w['cooldown']
                if cd > prev_mcd + 1: c['fired_mortar'] += 1
                prev_mcd = cd
    tot.update(c)
print(dict(sorted(tot.items())))
for wpn in ('gun', 'laser', 'mortar'):
    f = tot['fired_' + wpn]
    if f:
        print(wpn, 'fired', f, 'mech hits %.2f' % (tot[f'hit_mech_{wpn}'] / f), 'any hits %.2f' % (sum(v for k, v in tot.items() if k.startswith('hit_') and k.endswith(wpn)) / f))
