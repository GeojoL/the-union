# 握手协议（read-confirmed handshake）

machine-nodes 的健康验证**不看心跳**，看**读确认挑战-应答**：一方写出一份带 nonce 的内容，另一方**必须读到该内容**才能算出正确应答；任一环节断或对不上 = 该回合**硬 FAIL**。

## 角色

- **node**：主动方。持续出站 HTTP 打 center。
- **center**：被动方。持有每节点的握手状态 + ok/fail 计数。

> 为什么 node 主动 / HTTP：只要求节点**出站**可达 center，不要求 center 能反向连进节点（很多节点在 NAT 后 / 无公钥 / 是 Mac）。这是最低部署门槛。

## 一个回合（cycle）= 证两个方向

每 `HS_INTERVAL` 秒一回合，node 执行：

```
1. node 生成 nonce na;  写本地文件 chal-<seq>(na);  读回 na_read
2. node ──POST /hs/challenge {node,seq,challenge:na_read}──▶ center
3. center 把 challenge【写进文件】chal-<seq>，再【从文件读回】得 got；生成 center nonce cn；
   写文件 resp-<seq>(got,cn)；──▶ 回 {got_challenge:got, center_nonce:cn}
4. node 验  got_challenge == na   →  ✅【方向1】center 真读到了 node 的文件内容(node→center 送达确认)
5. node 把 cn【写进文件】resp-<seq>，读回 cn_read
6. node ──POST /hs/confirm {node,seq,confirm:cn_read,rtt_ms}──▶ center
7. center 读自己的 resp-<seq> 得 cn，验 confirm == cn → ✅【方向2】node 真读到了 center 的文件内容(center→node 送达确认)
8. center 记 DIR2_OK/FAIL；回 {ok}
9. node 记 OK / FAIL(带失败步骤)
```

**两个方向都用 nonce 读确认**：方向1 证 center 读了 node 的内容（node 出的 nonce 被原样读回），方向2 证 node 读了 center 的内容（center 出的 nonce 被原样回带）。

## 失败语义（无处藏）

任一步失败 = 该回合 `FAIL step=<步骤>`：

| step | 含义 |
|------|------|
| `post-challenge[...]` | node→center 的挑战 POST 失败（网络断/center 挂）|
| `DIR1-mismatch[got=..]` | center 回的 got_challenge ≠ na（内容被改/串号）|
| `DIR1-no-center-nonce` | center 没回 nonce（异常）|
| `post-confirm[...]` | 确认 POST 失败 |
| `DIR2-center-rejected` | center 验 confirm≠cn（node 读错/串号）|

**对账**：node 本地 `ok/fail` 与 center 的 `/hs/status` 里该节点 `ok/fail` 应一致（两机各自记，互证）。差值只应是「最后一回合时序错位」。

## 与旧法对比（为什么换）

| 旧（心跳 / 发送端 fail=0）| 新（本协议）|
|---|---|
| 只证「我在发」| 证「对方收到+读了」|
| 丢包发送端看不出（实测藏过 8 条）| 丢包=硬 FAIL 入 log |
| 单边自说 | 两机 nonce 互证 |

## 参数

| env | 默认 | 说明 |
|-----|------|------|
| `HS_INTERVAL` | 60 | 回合间隔（秒）|
| `HS_DURATION` | 43200 | 总时长（秒，12h）|
| `CENTER` | — | 节点必填，如 `http://192.168.50.50:8770` |

---

# The Union — 机群身份 / 节点表 / LAN 自动发现 / 一机多址（center 侧,2026-06-23）

> 握手协议（上）不变。本节是 center 在握手之上新增的【机群治理 + union-tui 数据契约】。

## 机群身份（cluster identity）

机群锚 = **ZeroTier 网络 nwid**（全球唯一 + 网络隔离 → 撞名结构上不可能）。本机群 = `88c5b1f339488c31`（"GeojoLu's Nodes"）。

- **center 自动锚定**：启动按 `env CENTER_CLUSTER_ID` > 本机唯一 ZT nwid（`/var/lib/zerotier-one/networks.d/*.conf`，零 sudo 可读）> 默认 `88c5b1f339488c31`。
- **节点名唯一性**：用 node 自报稳定指纹 `fp`（可选,建议 `sha256(machine-id ⊕ ZT-node-id)[:16]`）。同名同 fp = 同机刷新；**同名不同 fp = 撞名拒 409,不覆盖旧机**（保护既有节点）。fp 缺省 = `weak_fp`,宽松同名（向后兼容旧 installer）。
- **registry 信封**：顶层保留键 `_meta = {cluster_id, cluster_name, center_node, schema, updated}`；遍历节点跳过 `_` 前缀键。每节点条目 backfill `cluster_id/fp/fp_src/conflicts/source/addresses`。迁移惰性、幂等。

## 一机多址 + 在线判定

一台机器可有多地址（ZT `10.68.63.x` + 各 LAN 段）。center 按 **node 名归并** `addresses[]`，**在线判定被动优先**：

- `online = (now - last_seen < ONLINE_TTL[默认120s])`，用**不可伪造的 TCP 源 IP**（`self.client_address`,握手/注册时自动采集）。
- 任一地址最近主动探通 → 也算在线（主动 TCP 探测默认关 `PROBE_ENABLE=0`,避免 center→node 扫内网；多数 node 不监听端口）。
- **绝不因某地址探测失败否决被动在线**（治"卡 ZT 栏找不到某机"：ZT 那条 stale 但 LAN 条 recent → 整机 online）。

## HTTP 端点（向后兼容,只增不删）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/cluster` | **机群权威视图 = union-tui center 数据源**。见下 schema |
| GET | `/discovered` | LAN 组播发现的【未受信】节点(待人工 confirm) |
| POST | `/discovered/confirm` `{node}` | 人工把某 discovered 升 registered（**唯一授信路径**） |
| POST | `/discovered/reject` `{node}` | 移除 discovered + 短期抑制 |
| POST | `/register` | 增可选 `cluster_id`/`fp`/`addrs`/`port`；cluster_id 不匹配→409,fp 撞名→409 |
| GET | `/hs/status` | 兼容旧形状（过滤 `_meta`,只回节点条目） |

### `GET /cluster` 契约（Rust serde 映射用）

```json
{
  "cluster_id": "88c5b1f339488c31",
  "cluster_name": "GeojoLu's Nodes",
  "center_node": "aeb5d1aa97",
  "center": { "zt_ip": "10.68.63.93", "lan_ip": "192.168.50.50" },
  "schema": 2,
  "ts": "2026-06-23T10:19:39",
  "online_ttl_s": 120,
  "node_count": 1,
  "discovered_count": 0,
  "nodes": {
    "rasjol": {
      "node": "rasjol", "persona": "jade-wren-c0", "machine": "Linux aarch64",
      "ok": 3515, "fail": 0, "last_seen": "...", "last_rtt_ms": 12,
      "cluster_id": "88c5b1f339488c31", "fp": "", "fp_src": "legacy-backfill",
      "weak_fp": true, "cluster_match": true, "conflicts": 0, "source": "register",
      "online": true, "online_reason": "recent_seen",
      "addresses": [
        { "addr": "192.168.50.126", "kind": "lan", "port": null,
          "source": "handshake", "first_seen": "...", "last_seen": "...",
          "last_ok": null, "probe_state": "unknown" }
      ]
    }
  }
}
```

`online_reason ∈ {recent_seen, probe_up, stale}`；`addresses[].kind ∈ {zt, lan, other}`；`addresses[].probe_state ∈ {up, down, unknown}`。

### `GET /discovered` 契约

```json
{ "cluster_id": "88c5b1f339488c31", "count": 1,
  "nodes": [ { "node": "lannode1", "cluster_id": "...", "role": "node",
    "addr": "192.168.50.99", "src_addr": "192.168.50.50", "port": 9999,
    "first_seen": "...", "last_seen": "...", "count": 3, "status": "discovered",
    "addr_matches_src": false } ] }
```

`addr_matches_src=false` = 信标自报 addr 与 UDP 真实源 IP 不符（可疑,人工 confirm 时判断）。

## LAN 组播自动发现

- node/installer 周期（~15s）UDP 组播信标到 `239.255.63.70:48770`：`{cluster_id, node, role, addr, port}`。
- center 后台 daemon 监听，**只收 cluster_id 匹配本机群**的信标 → 登记 `discovered`（纯内存、有界 64 + TTL 120s）。
- **LAN 无鉴权 → 绝不自动信任**：discovered 永远只是线索,必须人工 `POST /discovered/confirm` 才进 registry。
- ⚠️ **LXC/Proxmox 组播可达性**：CT110 在 vmbr 桥后,跨宿主 LAN 节点的组播能否到达 center 需运维验证（IGMP snooping / 桥转发）；不通时 ZT 手配 + `/register` 仍是兜底（发现是【增强】非替代）。

## 安全边界

- `:8770` 绑 `0.0.0.0`、LAN+ZT 可达、**无鉴权**。cluster_id 是公开 nwid,只防误连/串扰,**不是鉴权凭据**（真隔离靠 ZT 入网 + guardian 防火墙 + 人工 confirm）。
- node 名 / seq 白名单（`NODE_RE`/`SEQ_RE`,挡路径穿越）；challenge/rtt clamp；DoS 有界（`MAX_NODES/MAX_BODY/MAX_DISCOVERED/MAX_ADDRS/MAX_BEACON`）。
- systemd 沙箱：`ProtectSystem=strict` + `ReadWritePaths=CENTER_HOME` + `ProtectHome=read-only` + `NoNewPrivileges`（即使再出路径 bug 也写不出 CENTER_HOME）。

## center 侧 env（全可覆盖,零依赖）

| env | 默认 | 说明 |
|-----|------|------|
| `CENTER_CLUSTER_ID` | 自动检测 | 机群 ZT nwid |
| `CENTER_MCAST_GRP` / `CENTER_MCAST_PORT` | `239.255.63.70` / `48770` | 组播组 |
| `CENTER_DISCOVERY` | `1` | LAN 发现开关 |
| `ONLINE_TTL_S` | `120` | 被动在线阈值 |
| `PROBE_ENABLE` / `PROBE_PORTS` | `0` / `8770,22` | 主动探测(默认关) |
| `MAX_NODES` / `MAX_DISCOVERED` / `MAX_ADDRS` | `64` / `64` / `16` | 有界 |
