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
import os, sys, json, time, secrets, socket, hashlib, subprocess, urllib.request, urllib.error

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
    t0 = time.monotonic()  # 单调钟测 RTT:不受 NTP 校时/挂起回拨影响
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
        return False, "post-challenge[%s]" % type(e).__name__, int((time.monotonic() - t0) * 1000)
    got = str(resp.get("got_challenge", "")); cnonce = str(resp.get("center_nonce", ""))
    # 验 center 真读了 node 的挑战(方向1)
    if got != na:
        return False, "DIR1-mismatch[got=%s]" % got[:8], int((time.monotonic() - t0) * 1000)
    if not cnonce:
        return False, "DIR1-no-center-nonce", int((time.monotonic() - t0) * 1000)
    # 3) node 把 center 的 nonce 写进文件 + 读回(证 node 真读了 center 的内容)
    rf = os.path.join(INp, "resp-%s.txt" % seq)
    with open(rf, "w") as f:
        f.write("seq=%s\ncenter_nonce=%s\nts=%s\n" % (seq, cnonce, ts()))
    cnonce_read = getv(rf, "center_nonce")
    rtt = int((time.monotonic() - t0) * 1000)
    # 4) POST 确认
    try:
        cr = post("/hs/confirm", {"node": NODE, "seq": seq, "confirm": cnonce_read, "rtt_ms": rtt})
    except Exception as e:
        return False, "post-confirm[%s]" % type(e).__name__, rtt
    if not cr.get("ok"):
        return False, "DIR2-center-rejected", rtt
    return True, "OK", rtt


def _machine_id():
    """跨平台机器唯一 id:Linux=/etc/machine-id;macOS=IOPlatformUUID;退化=hostname。"""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "IOPlatformUUID" in line:
                return line.split('"')[-2]
    except Exception:
        pass
    return socket.gethostname()


def _zt_node_id():
    """ZeroTier 10-hex 节点地址:zerotier-cli info 优先,退化读 identity.public;无 ZT 返回空。"""
    for cli in ("zerotier-cli", "/usr/local/bin/zerotier-cli",
                "/opt/homebrew/bin/zerotier-cli", "/usr/sbin/zerotier-cli"):
        try:
            parts = subprocess.run([cli, "info"], capture_output=True, text=True, timeout=5).stdout.split()
            if len(parts) >= 3 and parts[0] == "200":
                return parts[2]
        except Exception:
            pass
    for p in ("/var/lib/zerotier-one/identity.public",
              os.path.expanduser("~/Library/Application Support/ZeroTier/One/identity.public"),
              "/Library/Application Support/ZeroTier/One/identity.public"):
        try:
            with open(p) as f:
                return f.read().split(":", 1)[0].strip()
        except Exception:
            pass
    return ""


def node_fp():
    """节点指纹 = sha256(machine-id ⊕ ZT-node-id)[:16]。⊕=两串字节逐位 XOR,短串循环铺满长串。
    稳定(跨重启/重装不变)+ 唯一(机器维度)+ 节点自证(center 不可复算,仅存比对、撞 fp 拒)。
    无 ZT 时退化为 sha256(machine-id)[:16](仍稳定,弱一档由 center 判 weak_fp)。"""
    mid = _machine_id().encode()
    zt = _zt_node_id().encode()
    if not zt:
        xb = mid
    else:
        n = max(len(mid), len(zt))
        xb = bytes(mid[i % len(mid)] ^ zt[i % len(zt)] for i in range(n))
    return hashlib.sha256(xb).hexdigest()[:16]


def cluster_id():
    """机群锚 = ZT 网络 id。env CLUSTER_ID 优先,退化读 ~/.ccp-cluster。"""
    v = os.environ.get("CLUSTER_ID")
    if v:
        return v
    try:
        with open(os.path.expanduser("~/.ccp-cluster")) as f:
            for line in f:
                if line.startswith("cluster_id="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def register():
    try:
        mach = "%s %s" % (os.uname().sysname, os.uname().machine)
    except Exception:
        mach = "?"
    body = {"node": NODE, "persona": PERSONA, "machine": mach}
    fp = node_fp(); cid = cluster_id()
    if fp:
        body["fp"] = fp
    if cid:
        body["cluster_id"] = cid
    # 地址不自报:center 按 HTTP 连接源 IP 自采(防伪),节点经各网段打 center 即被自动累加归并到同一 fp
    try:
        post("/register", body, timeout=15)
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
    # ★单调钟做 soak 时长计:防 pre-NTP 启动/挂起唤醒令 wall-clock 跳变 → el 暴增 → 秒 DONE
    #   (2026-06-21 rasjol 实犯:某实例 DONE el=2804510s = wall-clock 回拨artifact)
    start = time.monotonic(); seq = 0; ok = 0; fail = 0
    while True:
        el = int(time.monotonic() - start)
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
