#!/usr/bin/env python3
"""machine-nodes / The Union center-agent — 中心机的握手对端 + 节点登记 + 机群身份 + LAN 自动发现 + 一机多址/在线聚合。
零依赖（python3 stdlib）。node 出站 HTTP 打过来,center 用【文件读确认】回应。

握手/登记(向后兼容,行为不变):
  POST /register        {node,persona,machine[,cluster_id,fp,addrs,port]}  -> 登记(增 cluster_id/fp 校验+多址采集)
  POST /hs/challenge    {node,seq,challenge}                -> 写 node 挑战到文件、读回、回 center nonce
  POST /hs/confirm      {node,seq,confirm[,rtt_ms]}         -> 读自己的 resp 文件验 node 确收、记 ok/fail
  GET  /hs/status                                           -> 每节点裸 registry(JSON,过滤 _meta,兼容旧消费者)
  GET  /                                                    -> 文本总览

The Union 新增(本轮):
  GET  /cluster                                             -> 机群权威视图: cluster_id + center + nodes[](含 addresses/online/fp) = union-tui center 数据源
  GET  /discovered                                          -> LAN 组播发现的【未受信】节点(待人工 confirm)
  POST /discovered/confirm  {node}                          -> 人工把某 discovered 升为 registered(唯一授信路径)
  POST /discovered/reject   {node}                          -> 移除某 discovered(并短期抑制)

机群身份(cluster identity):机群锚 = ZeroTier 网络 nwid(全球唯一+网络隔离)。center 启动按
  env CENTER_CLUSTER_ID > 本机唯一 ZT nwid(/var/lib/zerotier-one/networks.d/*.conf) > 默认 88c5b1f339488c31 自动锚定。
节点名唯一性:用 node 自报稳定指纹 fp(可选)——同名同 fp=同机刷新;同名不同 fp=撞名拒 409(不覆盖旧机)。

LAN 自动发现:后台 daemon 监听组播 239.255.63.70:48770,只收 cluster_id 匹配本机群的信标,登记为
  discovered(纯内存、不落盘、有界);LAN 无鉴权 → 绝不自动信任,必须人工一键 confirm 才进 registry。

一机多址 + 在线:按 node 名归并多地址(ZT+各 LAN 段);在线判定【被动优先】= 最近 last_seen < ONLINE_TTL
  即在线(用不可伪造的 TCP 源 IP),主动 TCP 探测默认关(避免 center→node 扫内网)。治"卡 ZT 栏找不到某机"。

环境(全可 env 覆盖,零依赖):
  CENTER_PORT(8770) CENTER_HOME(~/machine-nodes-center)
  CENTER_CLUSTER_ID(自动检测) CENTER_CLUSTER_NAME(GeojoLu's Nodes) CENTER_ZT_NODE/CENTER_ZT_IP/CENTER_LAN_IP
  CENTER_MCAST_GRP(239.255.63.70) CENTER_MCAST_PORT(48770) CENTER_DISCOVERY(1=开/0=关)
  ONLINE_TTL_S(120) PROBE_ENABLE(0) PROBE_PORTS(8770,22) MAX_NODES(64)
"""
import os, re, glob, json, time, socket, struct, secrets, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CENTER_PORT", "8770"))
HOME = os.path.expanduser(os.environ.get("CENTER_HOME", "~/machine-nodes-center"))
REG = os.path.join(HOME, "registry.json")
LOCK = threading.Lock()

# ── 机群身份 ──────────────────────────────────────────────────────────────
ZT_NETDIR = "/var/lib/zerotier-one/networks.d"
DEFAULT_CID = "88c5b1f339488c31"          # "GeojoLu's Nodes"
NWID_RE = re.compile(r"^[0-9a-f]{16}$")
SCHEMA = 2
RESERVED_PREFIX = "_"                       # registry 顶层保留键(如 _meta)以此前缀,遍历节点时跳过

# ── 安全/有界常量 ──────────────────────────────────────────────────────────
NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")  # node 名白名单(同时是目录名→挡路径穿越)
SEQ_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")               # seq 白名单(拼进 chal-/resp- 文件名→挡路径穿越+换行注入)
ROLE_RE = re.compile(r"^[A-Za-z0-9 ._:-]{0,32}$")
MAX_CHAL = 4096                                             # challenge 内容上限(攻击者可控,防任意大写盘)
MAX_BODY = int(os.environ.get("CENTER_MAX_BODY", str(64 * 1024)))   # 请求体上限
MAX_NODES = int(os.environ.get("MAX_NODES", "64"))                  # registered 节点软上限
FIELD_CLAMP = 128                                                   # persona/machine 等字段截断
MAX_ADDRS = int(os.environ.get("MAX_ADDRS", "16"))                  # per-node 地址上限(LRU)

# ── LAN 发现 ──────────────────────────────────────────────────────────────
MCAST_GRP = os.environ.get("CENTER_MCAST_GRP", "239.255.63.70")
MCAST_PORT = int(os.environ.get("CENTER_MCAST_PORT", "48770"))
DISCOVERY_ON = os.environ.get("CENTER_DISCOVERY", "1") != "0"
MAX_DISCOVERED = int(os.environ.get("MAX_DISCOVERED", "64"))
DISCOVERED_TTL = float(os.environ.get("DISCOVERED_TTL_S", "120"))
REJECT_TTL = float(os.environ.get("REJECT_TTL_S", "300"))
MAX_BEACON = 2048
DISCOVERED = {}        # node -> dict(纯内存)
REJECTED = {}          # node -> monotonic ts

# ── 一机多址/在线 ─────────────────────────────────────────────────────────
ONLINE_TTL = int(os.environ.get("ONLINE_TTL_S", "120"))
PROBE_ENABLE = os.environ.get("PROBE_ENABLE", "0") == "1"   # 主动 TCP 探测默认关
PROBE_PORTS = [int(p) for p in os.environ.get("PROBE_PORTS", "8770,22").split(",") if p.strip().isdigit()]
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT_S", "1.5"))


def detect_cluster_id():
    env = os.environ.get("CENTER_CLUSTER_ID", "").strip().lower()
    if NWID_RE.match(env):
        return env
    try:
        nwids = [os.path.basename(c)[:-5].lower() for c in glob.glob(os.path.join(ZT_NETDIR, "*.conf"))]
        nwids = [n for n in nwids if NWID_RE.match(n)]
        if len(nwids) == 1:
            return nwids[0]
        if len(nwids) > 1:
            print("WARN: 多个 ZT 网络 %r,无法确定机群,落默认 %s。请 env CENTER_CLUSTER_ID 显式指定" % (nwids, DEFAULT_CID), flush=True)
    except Exception:
        pass
    return DEFAULT_CID


CLUSTER_ID = detect_cluster_id()
CLUSTER_NAME = os.environ.get("CENTER_CLUSTER_NAME", "GeojoLu's Nodes")
CENTER_NODE = os.environ.get("CENTER_ZT_NODE", "aeb5d1aa97")
ZT_SELF = os.environ.get("CENTER_ZT_IP", "10.68.63.93")
LAN_SELF = os.environ.get("CENTER_LAN_IP", "192.168.50.50")


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _epoch(s):
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def clamp(v, n=FIELD_CLAMP):
    return str(v)[:n]


def ndir(node, *p):
    return os.path.join(HOME, "nodes", node, *p)


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


def node_items(r):
    """统一遍历入口:跳过保留键(如 _meta)。"""
    return [(k, v) for k, v in r.items() if not k.startswith(RESERVED_PREFIX)]


def valid_node(name):
    return bool(NODE_RE.match(name or ""))


def classify_addr(ip):
    if ip.startswith("10.68.63."):
        return "zt"
    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
        return "lan"
    return "other"


def ensure_meta(r):
    """幂等迁移:补 _meta + backfill 各节点新字段。返回是否改动(决定是否落盘)。"""
    changed = False
    m = r.get("_meta")
    if not isinstance(m, dict) or m.get("cluster_id") != CLUSTER_ID or m.get("schema") != SCHEMA:
        r["_meta"] = {"cluster_id": CLUSTER_ID, "cluster_name": CLUSTER_NAME,
                      "center_node": CENTER_NODE, "schema": SCHEMA, "updated": ts()}
        changed = True
    for _name, e in node_items(r):
        if "cluster_id" not in e:
            e["cluster_id"] = CLUSTER_ID; changed = True
        if "fp" not in e:
            e["fp"] = ""; e["fp_src"] = "legacy-backfill"; changed = True
        if "addresses" not in e:
            e["addresses"] = []; changed = True
        e.setdefault("conflicts", 0)
        e.setdefault("source", "register")
    return changed


def upsert_addr(entry, ip, source, port=None):
    """按 addr 去重 upsert + LRU 有界。被动采集的 client_address 是不可伪造的真源 IP。"""
    if not ip:
        return
    addrs = entry.setdefault("addresses", [])
    for a in addrs:
        if a.get("addr") == ip:
            a["last_seen"] = ts()
            if source:
                a["source"] = source
            if port:
                a["port"] = port
            return
    addrs.append({"addr": ip, "kind": classify_addr(ip), "port": port, "source": source,
                  "first_seen": ts(), "last_seen": ts(), "last_ok": None, "probe_state": "unknown"})
    if len(addrs) > MAX_ADDRS:
        addrs.sort(key=lambda x: _epoch(x.get("last_seen", "")))
        del addrs[: len(addrs) - MAX_ADDRS]


def compute_online(entry):
    """混合判据,被动优先:最近 last_seen<TTL → 在线;否则任一址最近探通 → 在线;否则 stale。绝不因探测失败否决被动在线。"""
    now = time.time()
    ls = entry.get("last_seen")
    if ls and now - _epoch(ls) < ONLINE_TTL:
        return True, "recent_seen"
    for a in entry.get("addresses", []):
        lo = a.get("last_ok")
        if lo and now - _epoch(lo) < ONLINE_TTL:
            return True, "probe_up"
    return False, "stale"


def _log(name, line):
    try:
        with open(os.path.join(HOME, name), "a") as f:
            f.write("[%s] %s\n" % (ts(), line))
    except Exception:
        pass


# ── LAN 组播发现 ──────────────────────────────────────────────────────────
def _prune_discovered(now):
    """调用前已持 LOCK。清陈旧 discovered/rejected。"""
    for n in [k for k, v in DISCOVERED.items() if now - v.get("_mono", 0) > DISCOVERED_TTL]:
        DISCOVERED.pop(n, None)
    for n in [k for k, t in REJECTED.items() if now - t > REJECT_TTL]:
        REJECTED.pop(n, None)


def _handle_beacon(raw, src_ip):
    """解析+严格校验+折叠登记。任何异常吞掉(永不崩线程)。"""
    if not raw or len(raw) > MAX_BEACON:
        return
    try:
        b = json.loads(raw.decode("utf-8", "strict"))
    except Exception:
        return
    if not isinstance(b, dict):
        return
    if str(b.get("cluster_id", "")) != CLUSTER_ID:      # 隔离闸:只收本机群
        return
    node = str(b.get("node", "")).strip()
    if not valid_node(node):
        return
    role = str(b.get("role", "")).strip()
    if not ROLE_RE.match(role):
        role = ""
    addr = str(b.get("addr", "")).strip()
    try:
        socket.inet_aton(addr)
        if addr.count(".") != 3:
            addr = src_ip
    except Exception:
        addr = src_ip                                    # 自报 addr 非法 → 退用真实源 IP
    try:
        port = int(b.get("port", 0))
    except Exception:
        return
    if not (1 <= port <= 65535):
        return
    now = time.monotonic()
    with LOCK:
        _prune_discovered(now)
        try:
            if node in load_reg():                       # 已受信注册 → 不重复发现
                return
        except Exception:
            pass
        if node in REJECTED:
            return
        cur = DISCOVERED.get(node)
        if cur is None:
            if len(DISCOVERED) >= MAX_DISCOVERED:         # 满 cap + 新 node → 丢弃防 DoS
                return
            cur = {"node": node, "first_seen": ts(), "count": 0, "status": "discovered"}
            DISCOVERED[node] = cur
        cur.update({"cluster_id": CLUSTER_ID, "role": role, "addr": addr, "src_addr": src_ip,
                    "port": port, "last_seen": ts(), "_mono": now})
        cur["count"] = cur.get("count", 0) + 1


def discovery_loop():
    while True:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.bind(("0.0.0.0", MCAST_PORT))
            mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.settimeout(5.0)
            print("discovery listening %s:%d cluster=%s" % (MCAST_GRP, MCAST_PORT, CLUSTER_ID), flush=True)
            while True:
                try:
                    raw, (src_ip, _sp) = s.recvfrom(MAX_BEACON + 64)
                except socket.timeout:
                    with LOCK:
                        _prune_discovered(time.monotonic())
                    continue
                _handle_beacon(raw, src_ip)
        except Exception as e:
            print("discovery loop error: %r (retry 5s)" % e, flush=True)
            time.sleep(5)
        finally:
            try:
                if s:
                    s.close()
            except Exception:
                pass


# ── 主动探测(默认关) ─────────────────────────────────────────────────────
def probe_scheduler():
    while True:
        time.sleep(max(15, ONLINE_TTL // 2))
        try:
            with LOCK:
                r = load_reg()
            targets = []
            for name, e in node_items(r):
                for a in e.get("addresses", []):
                    if a.get("kind") in ("zt", "lan") and a.get("addr") not in (ZT_SELF, LAN_SELF):
                        targets.append((name, a["addr"]))
            for name, ip in targets:
                ok = False
                for p in PROBE_PORTS:
                    try:
                        with socket.create_connection((ip, p), timeout=PROBE_TIMEOUT):
                            ok = True
                            break
                    except Exception:
                        pass
                if ok:
                    with LOCK:
                        r = load_reg(); e = r.get(name)
                        if e:
                            for a in e.get("addresses", []):
                                if a.get("addr") == ip:
                                    a["last_ok"] = ts(); a["probe_state"] = "up"
                            save_reg(r)
        except Exception as e:
            print("probe_scheduler error: %r" % e, flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, txt):
        b = txt.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n > MAX_BODY:                                   # 体过大 → 拒(防打爆内存)
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return None

    def _src(self):
        try:
            return self.client_address[0]
        except Exception:
            return ""

    # ── GET ──
    def do_GET(self):
        if self.path.startswith("/hs/status"):
            with LOCK:
                self._send(200, {k: v for k, v in load_reg().items() if not k.startswith(RESERVED_PREFIX)})
        elif self.path.startswith("/cluster"):
            self._cluster()
        elif self.path.startswith("/discovered"):
            self._discovered_get()
        elif self.path == "/" or self.path.startswith("/health"):
            self._overview()
        else:
            self._send(404, {"error": "not found"})

    def _cluster(self):
        with LOCK:
            r = load_reg()
            if ensure_meta(r):
                save_reg(r)
            m = r["_meta"]
            nodes = {}
            for name, e in node_items(r):
                d = dict(e)
                on, why = compute_online(e)
                d["online"], d["online_reason"] = on, why
                d["weak_fp"] = (e.get("fp", "") == "")
                d["cluster_match"] = (e.get("cluster_id", CLUSTER_ID) == CLUSTER_ID)
                nodes[name] = d
            disc_n = len(DISCOVERED)
        self._send(200, {"cluster_id": m["cluster_id"], "cluster_name": m.get("cluster_name", ""),
                         "center_node": m.get("center_node", ""), "schema": m.get("schema", SCHEMA),
                         "center": {"zt_ip": ZT_SELF, "lan_ip": LAN_SELF},
                         "ts": ts(), "online_ttl_s": ONLINE_TTL,
                         "node_count": len(nodes), "discovered_count": disc_n, "nodes": nodes})

    def _discovered_get(self):
        with LOCK:
            _prune_discovered(time.monotonic())
            out = []
            for d in sorted(DISCOVERED.values(), key=lambda x: x.get("last_seen", ""), reverse=True):
                o = {k: v for k, v in d.items() if not k.startswith("_")}
                o["addr_matches_src"] = (o.get("addr") == o.get("src_addr"))
                out.append(o)
        self._send(200, {"cluster_id": CLUSTER_ID, "count": len(out), "nodes": out})

    def _overview(self):
        with LOCK:
            r = load_reg()
        items = node_items(r)
        tot_ok = sum(n.get("ok", 0) for _k, n in items)
        tot_fail = sum(n.get("fail", 0) for _k, n in items)
        txt = "The Union center @%s\ncluster=%s (%s)  nodes=%d ok=%d fail=%d  discovered=%d\n" % (
            ts(), CLUSTER_ID, CLUSTER_NAME, len(items), tot_ok, tot_fail, len(DISCOVERED))
        for name, n in items:
            on, why = compute_online(n)
            txt += "  %-16s persona=%-12s %-7s ok=%d fail=%d last=%s rtt=%sms addrs=%d%s\n" % (
                name, n.get("persona", "?"), "ONLINE" if on else "offline",
                n.get("ok", 0), n.get("fail", 0), n.get("last_seen", "?"),
                n.get("last_rtt_ms", "?"), len(n.get("addresses", [])),
                ("  fp=%s" % n.get("fp_src", "?")) if n.get("conflicts") else "")
        self._text(200, txt)

    # ── POST ──
    def do_POST(self):
        b = self._body()
        if b is None:
            return self._send(400, {"error": "bad json or body too large"})
        node = str(b.get("node", "")).strip()

        if self.path == "/register":
            return self._register(b, node)
        if self.path == "/discovered/confirm":
            return self._discovered_confirm(node)
        if self.path == "/discovered/reject":
            return self._discovered_reject(node)

        # 握手两端点:node 必填 + 名校验(挡路径穿越)
        if not node:
            return self._send(400, {"error": "node required"})
        if not valid_node(node):
            return self._send(400, {"error": "bad node name"})
        # seq 也拼进 chal-/resp- 文件名 → 白名单(挡 seq 路径穿越 + 换行注入)
        seq = str(b.get("seq", "")).strip()
        if not SEQ_RE.match(seq):
            return self._send(400, {"error": "bad seq"})

        if self.path == "/hs/challenge":
            return self._hs_challenge(b, node, seq)
        if self.path == "/hs/confirm":
            return self._hs_confirm(b, node, seq)
        return self._send(404, {"error": "not found"})

    def _register(self, b, node):
        if not node:
            return self._send(400, {"error": "node required"})
        if not valid_node(node):
            return self._send(400, {"error": "bad node name"})
        bcid = str(b.get("cluster_id", "")).strip().lower()
        if bcid and bcid != CLUSTER_ID:                    # 显式带且不匹配 → 拒
            _log("rejects.log", "cluster_mismatch node=%s got=%s" % (node, bcid))
            return self._send(409, {"ok": False, "error": "cluster_mismatch",
                                    "expected": CLUSTER_ID, "got": bcid})
        bfp = clamp(b.get("fp", ""), 64).strip()
        src = self._src()
        with LOCK:
            r = load_reg(); ensure_meta(r)
            cur = r.get(node)
            # 撞名冲突:两边 fp 都非空且不同 → 拒,不覆盖旧机
            if cur and cur.get("fp") and bfp and bfp != cur["fp"]:
                cur["conflicts"] = cur.get("conflicts", 0) + 1
                cur["last_conflict"] = ts(); r[node] = cur; save_reg(r)
                _log("conflicts.log", "CONFLICT node=%s existing=%s incoming=%s" % (node, cur["fp"][:12], bfp[:12]))
                return self._send(409, {"ok": False, "error": "node_name_conflict", "node": node,
                                        "existing_fp": cur["fp"][:8], "your_fp": bfp[:8],
                                        "hint": "name taken by another machine in this cluster"})
            # 新节点 + 节点数上限(防 LAN 刷名耗 inode)
            if cur is None and len([1 for _k, _v in node_items(r)]) >= MAX_NODES:
                _log("rejects.log", "max_nodes node=%s" % node)
                return self._send(429, {"ok": False, "error": "max_nodes", "limit": MAX_NODES})
            os.makedirs(ndir(node, "in"), exist_ok=True)
            os.makedirs(ndir(node, "out"), exist_ok=True)
            cur = cur or {"ok": 0, "fail": 0}
            if bfp and not cur.get("fp"):                  # TOFU 占位/backfill
                cur["fp"] = bfp; cur["fp_src"] = "node-reported"
                _log("conflicts.log", "fp-bound node=%s fp=%s" % (node, bfp[:12]))
            cur.update({"persona": clamp(b.get("persona", "?")), "machine": clamp(b.get("machine", "?")),
                        "cluster_id": CLUSTER_ID, "registered": ts(), "center_seen": ts()})
            upsert_addr(cur, src, "client_address", b.get("port") or None)   # 不可伪造的被动源 IP
            for ip in (b.get("addrs") or [])[:MAX_ADDRS]:                    # node 自报候选地址
                ip = str(ip).strip()
                if classify_addr(ip) in ("zt", "lan"):
                    upsert_addr(cur, ip, "register")
            cur.setdefault("ok", 0); cur.setdefault("fail", 0)
            cur.setdefault("fp", ""); cur.setdefault("fp_src", "legacy-backfill")
            cur.setdefault("conflicts", 0); cur.setdefault("source", "register")
            r[node] = cur; save_reg(r)
        return self._send(200, {"ok": True, "node": node, "cluster_id": CLUSTER_ID,
                                "fp_known": bool(cur.get("fp"))})

    def _hs_challenge(self, b, node, seq):
        chal = str(b.get("challenge", ""))[:MAX_CHAL]       # 攻击者可控 → 有界
        if not chal:
            return self._send(400, {"error": "challenge required"})
        src = self._src()
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
            # 被动在线:握手源 IP 刷新地址 last_seen(免 node 改协议)
            r = load_reg()
            if node in r:
                upsert_addr(r[node], src, "handshake")
                save_reg(r)
        return self._send(200, {"got_challenge": got, "center_nonce": cnonce, "seq": seq})

    def _hs_confirm(self, b, node, seq):
        confirm = str(b.get("confirm", ""))[:128]
        rtt = b.get("rtt_ms")
        if rtt is not None:                                 # 数值化校验,防超长/嵌套对象污染 registry
            try:
                rtt = round(float(rtt), 3)
            except (TypeError, ValueError):
                rtt = None
        src = self._src()
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
            r = load_reg(); ensure_meta(r)
            cur = r.get(node, {"ok": 0, "fail": 0, "persona": "?", "machine": "?"})
            cur["ok"] = cur.get("ok", 0) + (1 if ok else 0)
            cur["fail"] = cur.get("fail", 0) + (0 if ok else 1)
            cur["last_seen"] = ts()
            if rtt is not None:
                cur["last_rtt_ms"] = rtt
            upsert_addr(cur, src, "handshake")
            r[node] = cur; save_reg(r)
            with open(ndir(node, "center.log"), "a") as f:
                f.write("[%s] seq=%s %s confirm=%s expect=%s\n" % (
                    ts(), seq, "DIR2_OK" if ok else "DIR2_FAIL", confirm[:12], cnonce[:12]))
            for sub in ("in", "out"):
                try:
                    files = sorted(os.listdir(ndir(node, sub)), key=lambda x: os.path.getmtime(ndir(node, sub, x)))
                    for old in files[:-80]:
                        os.remove(ndir(node, sub, old))
                except Exception:
                    pass
        return self._send(200, {"ok": ok})

    def _discovered_confirm(self, node):
        """人工把 LAN 发现的节点升为 registered = 唯一授信路径。"""
        if not valid_node(node):
            return self._send(400, {"error": "bad node name"})
        with LOCK:
            d = DISCOVERED.get(node)
            if d is None:
                return self._send(404, {"error": "not discovered"})
            r = load_reg(); ensure_meta(r)
            if node in r:                                  # 已注册 → 不覆盖既有受信条目
                DISCOVERED.pop(node, None)
                return self._send(409, {"error": "already registered"})
            if len([1 for _k, _v in node_items(r)]) >= MAX_NODES:
                return self._send(429, {"error": "max_nodes", "limit": MAX_NODES})
            os.makedirs(ndir(node, "in"), exist_ok=True)
            os.makedirs(ndir(node, "out"), exist_ok=True)
            cur = {"ok": 0, "fail": 0, "persona": "?", "machine": "%s:%s" % (d.get("addr", "?"), d.get("port", "?")),
                   "cluster_id": CLUSTER_ID, "source": "discovery", "fp": "", "fp_src": "discovery",
                   "conflicts": 0, "addresses": [], "registered": ts(), "center_seen": ts()}
            if classify_addr(str(d.get("addr", ""))) in ("zt", "lan"):
                upsert_addr(cur, str(d["addr"]), "beacon", d.get("port"))
            r[node] = cur; save_reg(r)
            DISCOVERED.pop(node, None)
            _log("rejects.log", "confirmed node=%s addr=%s" % (node, d.get("addr")))
        return self._send(200, {"ok": True, "node": node, "promoted": True})

    def _discovered_reject(self, node):
        if not valid_node(node):
            return self._send(400, {"error": "bad node name"})
        with LOCK:
            removed = DISCOVERED.pop(node, None) is not None
            REJECTED[node] = time.monotonic()
        return self._send(200, {"ok": True, "removed": removed})


def main():
    os.makedirs(os.path.join(HOME, "nodes"), exist_ok=True)
    with LOCK:
        r = load_reg()
        if ensure_meta(r) or not os.path.exists(REG):
            save_reg(r)
    if DISCOVERY_ON:
        threading.Thread(target=discovery_loop, name="discovery", daemon=True).start()
    if PROBE_ENABLE:
        threading.Thread(target=probe_scheduler, name="probe", daemon=True).start()
    print("The Union center-agent :%d home=%s cluster=%s node=%s discovery=%s probe=%s" % (
        PORT, HOME, CLUSTER_ID, CENTER_NODE, DISCOVERY_ON, PROBE_ENABLE), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
