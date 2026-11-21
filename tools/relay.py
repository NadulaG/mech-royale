"""Play the bot in a real server match through the browser game's WebSocket (private room by default).

    python3 tools/relay.py                       # new private room, our bot as the only player
    python3 tools/relay.py --code ABCDE          # join an existing private room
    python3 tools/relay.py --matches 3 --out runs/

Records every server message to <out>/<code>-<n>.jsonl.gz and the bot's stderr to <out>/<code>-<n>.log.
"""
import argparse
import asyncio
import gzip
import json
import os
import random
import string
import sys
import time
import urllib.request

import ssl

import websockets

try:
    import certifi
    SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL = ssl.create_default_context()

HOST = "play.joinbuilders.org"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
KIT = os.path.dirname(REPO)


def http_json(path):
    with urllib.request.urlopen(f"https://{HOST}{path}", timeout=6, context=SSL) as r:
        return json.loads(r.read())


async def run_match(args, sid, room, code, n):
    q = {"token": "", "sid": sid, "name": args.name}
    if code:
        q["code"] = code
    url = f"wss://{HOST}/r/{room}/play?" + "&".join(f"{k}={v}" for k, v in q.items())
    os.makedirs(args.out, exist_ok=True)
    tag = f"{code or room}-{n}-{int(time.time())}"
    rec = gzip.open(os.path.join(args.out, tag + ".jsonl.gz"), "wt")
    errlog = open(os.path.join(args.out, tag + ".log"), "w")
    print(f"connecting {url}\nwatch: https://{HOST}/game.html?watch&code={code}" if code else f"connecting {url}", flush=True)
    bot = None
    started = False
    result = None
    async with websockets.connect(url, max_size=None, ping_interval=20, ssl=SSL) as ws:
        sent_ready = False
        last_lobby = 0

        async def pump_bot():
            while True:
                line = await bot.stdout.readline()
                if not line:
                    return
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("type") == "ready":
                    continue
                dbg = m.pop("debug", None)
                await ws.send(json.dumps(m, separators=(",", ":")))
                rec.write(json.dumps({"type": "act", "tick": m.get("tick"), "a": {k: v for k, v in m.items() if k != "tick"},
                                      "label": (dbg or {}).get("label")}, separators=(",", ":")) + "\n")

        pump = None
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            ty = msg.get("type")
            if ty != "lobby":
                rec.write(raw if isinstance(raw, str) else raw.decode())
                rec.write("\n")
            if ty == "lobby":
                you = msg.get("you", {})
                if time.time() - last_lobby > 5:
                    last_lobby = time.time()
                    print("lobby", msg.get("phase"), "host" if you.get("host") else "", "ready" if you.get("ready") else "",
                          {k: msg.get(k) for k in ("match", "seats", "next_at")}, flush=True)
                if not you.get("ready") and not sent_ready:
                    await ws.send(json.dumps({"type": "ready", "name": args.name}))
                    sent_ready = True
                if you.get("host") and msg.get("phase") == "waiting" and not started:
                    await ws.send(json.dumps({"type": "start"}))
                    started = True
                g = msg.get("gather")
                if g and not started and you.get("ready"):
                    await ws.send(json.dumps({"type": "start"}))
                    started = True
            elif ty == "hello":
                print(f"hello slot {msg.get('slot')}", flush=True)
                msg["map"]["dir"] = os.path.join(KIT, "arena")
                bot = await asyncio.create_subprocess_exec(*args.cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                           stderr=errlog, cwd=REPO)
                bot.stdin.write((json.dumps(msg) + "\n").encode())
                await bot.stdin.drain()
                pump = asyncio.create_task(pump_bot())
                await ws.send(json.dumps({"type": "loaded"}))
            elif ty == "obs":
                if bot is not None:
                    bot.stdin.write((raw if isinstance(raw, str) else raw.decode()).encode() + b"\n")
                    await bot.stdin.drain()
                if msg["tick"] % 200 == 0:
                    s = msg["self"]
                    print(f"t={msg['t']:.0f} alive={msg['alive']} hp={s['hp']:.0f} cubes={s['cubes']} w={[w and w['kind'] for w in s['weapons']]} "
                          f"seen={[m['id'] for m in msg['mechs'] if m['kind']=='mech']}", flush=True)
            elif ty == "end":
                print("END", msg, flush=True)
                result = msg
                if bot is not None:
                    try:
                        bot.stdin.write((json.dumps(msg) + "\n").encode())
                        await bot.stdin.drain()
                        bot.stdin.close()
                    except Exception:
                        pass
                if not args.wait_results:
                    break
            elif ty == "results":
                print("RESULTS", json.dumps(msg)[:1500], flush=True)
                break
    rec.close()
    if bot is not None:
        try:
            await asyncio.wait_for(bot.wait(), 3)
        except asyncio.TimeoutError:
            bot.kill()
    errlog.close()
    return result


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--matches", type=int, default=1)
    ap.add_argument("--name", default="ohio-steel")
    ap.add_argument("--out", default=os.path.join(REPO, "tools", "runs"))
    ap.add_argument("--wait-results", action="store_true")
    ap.add_argument("cmd", nargs="*", default=["python3", "-u", "bot.py"])
    args = ap.parse_args()
    if not args.cmd:
        args.cmd = ["python3", "-u", "bot.py"]
    sid = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
    places = []
    for n in range(args.matches):
        code, room = args.code, None
        if args.public:
            room = http_json("/mm/pick?lead=0")["room"]
            code = None
        elif code:
            room = http_json(f"/mm/code/join?c={code}&sid={sid}")["room"]
        else:
            r = http_json(f"/mm/code/new?sid={sid}")
            code, room = r["code"], r["room"]
        res = await run_match(args, sid, room, code, n)
        if res:
            places.append(res.get("place"))
        print("places so far", places, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
