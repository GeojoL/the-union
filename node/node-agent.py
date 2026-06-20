#!/usr/bin/env python3
"""machine-nodes node-agent — 节点机的握手 agent（出站 HTTP 打 center）。
零依赖（python3 stdlib）。每回合证两个方向【读到对方文件内容】:
  1) node 新建挑战文件(nonce na)→ 读回 → POST /hs/challenge
  2) center 把它写进文件+读回+回 center_nonce → node 验 got_challenge==na (=center 真读了 node 的文件)
  3) node 把 center_nonce 写进文件+读回 → POST /hs/confirm
  4) center 验 confirm==center_nonce (=node 真读了 center 的文件) → 回 {ok:true}
任一环节断/对不上 = 该回合硬 FAIL（带失败步骤）入 log。
环境(必填 CENTER):
  CENTER=http://<center-ip>:8770   NODE=<名>   PERSONA=<人格>
  HS_INTERVAL=60   HS_DURATION=43200   NODE_HOME=~/machine-nodes-node
停: touch $NODE_HOME/handshake.stop
"""
import os, sys, json, time, secrets, socket, urllib.request, urllib.error

CENTER = os.environ.get("CENTER", "").rstrip("/")
NODE = os.environ.get("NODE") or socket.gethostname().split(".")[0]
PERSONA = os.environ.get("PERSONA", "?")
INTERVAL = int(os.environ.get("HS_INTERVAL", "60"))
DURATION = int(os.environ.get("HS_DURATION", "43200"))
HOME = os.path.expanduser(os.environ.get("NODE_HOME", "~/machine-nodes-node"))
OUT = os.path.join(HOME, "out"); INp = os.path.join(HOME, "in")
LOG = os.path.join(HOME, "handshake.log"); STAT = os.path.join(HOME, "handshake.stat")
STOP = os.path.join(HOME, "handshake.stop")


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def nonce():
    return secrets.token_hex(16)


def post(path, obj, timeout=20):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(CENTER + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def getv(fn, key):
    with open(fn) as f:
        for line in f:
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return ""


def do_cycle(seq):
    """return (ok:bool, step:str, rtt_ms:int)"""
    t0 = time.time()
    na = nonce()
    # 1) node 新建挑战文件 + 读回(证 node 自己也走文件)
    cf = os.path.join(OUT, "chal-%s.txt" % seq)
    with open(cf, "w") as f:
        f.write("seq=%s\nna=%s\nts=%s\n" % (seq, na, ts()))
    na_read = getv(cf, "na")
    # 2) POST 挑战
    try:
        resp = post("/hs/challenge", {"node": NODE, "seq": seq, "challenge": na_read})
    except Exception as e:
        return False, "post-challenge[%s]" % type(e).__name__, int((time.time() - t0) * 1000)
    got = str(resp.get("got_challenge", "")); cnonce = str(resp.get("center_nonce", ""))
    # 验 center 真读了 node 的挑战(方向1)
    if got != na:
        return False, "DIR1-mismatch[got=%s]" % got[:8], int((time.time() - t0) * 1000)
    if not cnonce:
        return False, "DIR1-no-center-nonce", int((time.time() - t0) * 1000)
    # 3) node 把 center 的 nonce 写进文件 + 读回(证 node 真读了 center 的内容)
    rf = os.path.join(INp, "resp-%s.txt" % seq)
    with open(rf, "w") as f:
        f.write("seq=%s\ncenter_nonce=%s\nts=%s\n" % (seq, cnonce, ts()))
    cnonce_read = getv(rf, "center_nonce")
    rtt = int((time.time() - t0) * 1000)
    # 4) POST 确认
    try:
        cr = post("/hs/confirm", {"node": NODE, "seq": seq, "confirm": cnonce_read, "rtt_ms": rtt})
    except Exception as e:
        return False, "post-confirm[%s]" % type(e).__name__, rtt
    if not cr.get("ok"):
        return False, "DIR2-center-rejected", rtt
    return True, "OK", rtt


def register():
    try:
        mach = "%s %s" % (os.uname().sysname, os.uname().machine)
    except Exception:
        mach = "?"
    try:
        post("/register", {"node": NODE, "persona": PERSONA, "machine": mach}, timeout=15)
        return True
    except Exception as e:
        with open(LOG, "a") as f:
            f.write("[%s] REGISTER FAIL %s (center=%s)\n" % (ts(), e, CENTER))
        return False


def main():
    if not CENTER:
        print("CENTER env required (e.g. http://192.168.50.50:8770)", file=sys.stderr); sys.exit(2)
    os.makedirs(OUT, exist_ok=True); os.makedirs(INp, exist_ok=True)
    if os.path.exists(STOP):
        os.remove(STOP)
    with open(LOG, "a") as f:
        f.write("[%s] NODE START node=%s persona=%s center=%s interval=%ss duration=%ss\n" % (
            ts(), NODE, PERSONA, CENTER, INTERVAL, DURATION))
    register()
    start = time.time(); seq = 0; ok = 0; fail = 0
    while True:
        el = int(time.time() - start)
        if el >= DURATION:
            with open(LOG, "a") as f:
                f.write("[%s] DONE el=%ss ok=%d fail=%d\n" % (ts(), el, ok, fail)); break
        if os.path.exists(STOP):
            with open(LOG, "a") as f:
                f.write("[%s] STOPPED el=%ss ok=%d fail=%d\n" % (ts(), el, ok, fail)); break
        seq += 1
        good, step, rtt = do_cycle(seq)
        if good:
            ok += 1
            line = "[%s] seq=%d OK %dms (双向读确认)" % (ts(), seq, rtt)
        else:
            fail += 1
            line = "[%s] seq=%d FAIL step=%s %dms" % (ts(), seq, step, rtt)
        with open(LOG, "a") as f:
            f.write(line + "\n")
        with open(STAT, "w") as f:
            f.write("node=%s ok=%d fail=%d seq=%d el=%ss last=%s\n" % (NODE, ok, fail, seq, el, ts()))
        # 本地清理(留最近 80)
        for d in (OUT, INp):
            try:
                files = sorted(os.listdir(d), key=lambda x: os.path.getmtime(os.path.join(d, x)))
                for old in files[:-80]:
                    os.remove(os.path.join(d, old))
            except Exception:
                pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
