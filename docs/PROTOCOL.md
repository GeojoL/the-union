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

## 注册与身份（/register · cluster_id + fp · 多地址归一）

`POST /register` body（center schema≥2；旧字段全兼容，旧 center 忽略新字段）：

```json
{ "node":"macjol", "persona":"coral-marten-10", "machine":"Darwin arm64",
  "cluster_id":"88c5b1f339488c31", "fp":"a3e9653b6bc2e487" }
```

- **`cluster_id`** = ZT 网络 id（节点读 `~/.ccp-cluster` 或 env `CLUSTER_ID`）。机群锚,全球唯一+隔离,**不会和别人机群撞名**;center 校 `cluster_match`。
- **`fp`(节点指纹)** = `sha256(machine-id ⊕ ZT-node-id)[:16]`。
  - `machine-id`:Linux=`/etc/machine-id`,macOS=`IOPlatformUUID`(ioreg),退化 hostname。
  - `ZT-node-id`:`zerotier-cli info` 第3字段(或 `identity.public` 首段);无 ZT 退化为 `sha256(machine-id)[:16]`。
  - `⊕`:两串 ASCII 字节**逐位 XOR,短串循环铺满长串**,再 sha256 取前16 hex。确定性、跨重启/重装稳定、机器维度唯一。
  - **节点自证、center 不复算**:center 仅存 fp 比对——同名不同 fp = 冒名/换机 → **409 拒、不覆盖**(`conflicts`计数);`fp_src=node-reported`(真报)/`legacy-backfill`(旧节点补)/`weak_fp`(无ZT退化)。
- **地址不自报**:body 里**不放 addresses**——center 用 **HTTP 连接的源 IP**自采(防伪,自报地址会被忽略)。
  - **多地址归一台机**:同一节点经不同网段(ZT 10.68.63.x / LAN 192.168.x / 别的段)分别打 center,各次源 IP 被**按 fp 归并到同一节点条目**,`kind` 按子网判(zt/lan)。
  - 实测:macjol 经 ZT(`10.68.63.93`)+ LAN(`192.168.50.50`)各注册一次 → 同一条目挂 `10.68.63.60(zt)`+`192.168.50.51(lan)`,`conflicts:0`。解决"机器有多地址、center 卡某栏找不到"。
- **`online`** = 任一地址近 `online_ttl_s`(默120s)有活动(注册/握手/被动收包);一次性 register 不持续握手 → 很快 `stale`。要长亮需 node-agent 持续握手回合。

> LAN **组播自动发现**(beacon→`/discovered`)是「发现未知新节点」的增强,与本注册路径正交;跨 Proxmox 宿主组播会被网桥丢,待桥修(需机主点头)。**ZT/LAN 单播 + /register 不依赖组播,现可用。**
