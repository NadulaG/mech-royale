"""Feed the recorded check stream straight into Bot.act and print actions / timing / exceptions."""
import gzip, json, os, sys, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import bot as B

kit = os.path.abspath(os.path.join(HERE, "..", ".."))
lines = gzip.open(os.path.join(kit, "check", "stream.jsonl.gz"), "rt").read().splitlines()
hello = json.loads(lines[0])
hello["map"]["dir"] = os.path.join(kit, "arena")
b = B.Runner(hello)
every = int(sys.argv[1]) if len(sys.argv) > 1 else 25
times = []
for l in lines[1:]:
    o = json.loads(l)
    if o["type"] != "obs":
        continue
    t0 = time.perf_counter()
    try:
        a = b.act(o)
    except Exception:
        traceback.print_exc(); break
    times.append((time.perf_counter() - t0) * 1000)
    if o["tick"] % (every * 2) == 0:
        print(o["t"], b.mode, {k: v for k, v in a.items() if k != "debug"})
times.sort()
print("ms p50 %.2f p99 %.2f max %.2f" % (times[len(times)//2], times[int(len(times)*.99)], times[-1]))
