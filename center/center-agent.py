#!/usr/bin/env python3
"""machine-nodes center-agent — 中心机的握手对端 + 节点登记 + ok/fail 聚合。
零依赖（python3 stdlib）。node 出站 HTTP 打过来,center 用【文件读确认】回应:
  POST /register        {node,persona,machine}              -> 登记
  POST /hs/challenge    {node,seq,challenge}                -> 写 node 挑战到文件、读回、回 center nonce
  POST /hs/confirm      {node,seq,confirm}                  -> 读自己的 resp 文件验 node 确收、记 ok/fail
  GET  /hs/status                                           -> 每节点 ok/fail/last-seen（JSON）
  GET  /                                                    -> 文本总览
环境: CENTER_PORT(默认8770) CENTER_HOME(默认 ~/machine-nodes-center)
"""
import os, json, time, secrets, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CENTER_PORT", "8770"))
HOME = os.path.expanduser(os.environ.get("CENTER_HOME", "~/machine-nodes-center"))
REG = os.path.join(HOME, "registry.json")
LOCK = threading.Lock()


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def ndir(node, *p):
    d = os.path.join(HOME, "nodes", node, *p)
    return d


def load_reg():
    try:
        with open(REG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_reg(r):
    tmp = REG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(r, f, indent=2, ensure_ascii=False)
    os.replace(tmp, REG)


def nonce():
    return secrets.token_hex(16)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默,自有 log

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return None

    def do_GET(self):
        if self.path.startswith("/hs/status"):
            with LOCK:
                self._send(200, load_reg())
        elif self.path == "/" or self.path.startswith("/health"):
            r = load_reg()
            tot_ok = sum(n.get("ok", 0) for n in r.values())
            tot_fail = sum(n.get("fail", 0) for n in r.values())
            txt = "machine-nodes center @%s\nnodes=%d ok=%d fail=%d\n" % (ts(), len(r), tot_ok, tot_fail)
            for name, n in r.items():
                txt += "  %-16s persona=%-12s ok=%d fail=%d last=%s rtt=%sms\n" % (
                    name, n.get("persona", "?"), n.get("ok", 0), n.get("fail", 0),
                    n.get("last_seen", "?"), n.get("last_rtt_ms", "?"))
            b = txt.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        b = self._body()
        if b is None:
            return self._send(400, {"error": "bad json"})
        node = str(b.get("node", "")).strip()
        if not node and self.path != "/register":
            return self._send(400, {"error": "node required"})

        if self.path == "/register":
            node = str(b.get("node", "")).strip()
            if not node:
                return self._send(400, {"error": "node required"})
            with LOCK:
                os.makedirs(ndir(node, "in"), exist_ok=True)
                os.makedirs(ndir(node, "out"), exist_ok=True)
                r = load_reg()
                cur = r.get(node, {"ok": 0, "fail": 0})
                cur.update({"persona": b.get("persona", "?"), "machine": b.get("machine", "?"),
                            "registered": ts(), "center_seen": ts()})
                cur.setdefault("ok", 0); cur.setdefault("fail", 0)
                r[node] = cur
                save_reg(r)
            return self._send(200, {"ok": True, "node": node})

        if self.path == "/hs/challenge":
            seq = b.get("seq"); chal = str(b.get("challenge", ""))
            if not chal:
                return self._send(400, {"error": "challenge required"})
            # ★文件读确认:把 node 的挑战【写进文件】,再【从文件读回】拿到 nonce(证 center 真读了 node 的文件)
            with LOCK:
                os.makedirs(ndir(node, "in"), exist_ok=True)
                os.makedirs(ndir(node, "out"), exist_ok=True)
                cf = ndir(node, "in", "chal-%s.txt" % seq)
                with open(cf, "w") as f:
                    f.write("seq=%s\nchallenge=%s\nts=%s\n" % (seq, chal, ts()))
                got = ""
                with open(cf) as f:
                    for line in f:
                        if line.startswith("challenge="):
                            got = line.split("=", 1)[1].strip()
                cnonce = nonce()
                rf = ndir(node, "out", "resp-%s.txt" % seq)
                with open(rf, "w") as f:
                    f.write("seq=%s\ngot_challenge=%s\ncenter_nonce=%s\nts=%s\n" % (seq, got, cnonce, ts()))
            return self._send(200, {"got_challenge": got, "center_nonce": cnonce, "seq": seq})

        if self.path == "/hs/confirm":
            seq = b.get("seq"); confirm = str(b.get("confirm", ""))
            rtt = b.get("rtt_ms")
            with LOCK:
                rf = ndir(node, "out", "resp-%s.txt" % seq)
                cnonce = ""
                try:
                    with open(rf) as f:
                        for line in f:
                            if line.startswith("center_nonce="):
                                cnonce = line.split("=", 1)[1].strip()
                except Exception:
                    pass
                ok = bool(cnonce) and confirm == cnonce
                r = load_reg()
                cur = r.get(node, {"ok": 0, "fail": 0, "persona": "?", "machine": "?"})
                cur["ok"] = cur.get("ok", 0) + (1 if ok else 0)
                cur["fail"] = cur.get("fail", 0) + (0 if ok else 1)
                cur["last_seen"] = ts()
                if rtt is not None:
                    cur["last_rtt_ms"] = rtt
                r[node] = cur
                save_reg(r)
                # 逐回合 log
                lg = ndir(node, "center.log")
                with open(lg, "a") as f:
                    f.write("[%s] seq=%s %s confirm=%s expect=%s\n" % (
                        ts(), seq, "DIR2_OK" if ok else "DIR2_FAIL", confirm[:12], cnonce[:12]))
                # 清理旧件(留最近 80)
                for sub in ("in", "out"):
                    try:
                        files = sorted(os.listdir(ndir(node, sub)), key=lambda x: os.path.getmtime(ndir(node, sub, x)))
                        for old in files[:-80]:
                            os.remove(ndir(node, sub, old))
                    except Exception:
                        pass
            return self._send(200, {"ok": ok})

        return self._send(404, {"error": "not found"})


def main():
    os.makedirs(os.path.join(HOME, "nodes"), exist_ok=True)
    if not os.path.exists(REG):
        save_reg({})
    print("machine-nodes center-agent listening on 0.0.0.0:%d  home=%s" % (PORT, HOME), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
