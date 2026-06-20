# machine-nodes

**中心机 ↔ 分布节点** 的一键部署 + 读确认链路验证工具集。
一条命令把任意机器装成一个 **node**,自动连上 **center**,并跑**文件级双向读确认握手**来证明跨机通讯真的通（不是看心跳）。

> 由 najol（集群中心）开发。诞生背景：心跳/发送端 `fail=0` 会把丢包藏起来（实测抓到过 macjol→najol 静默丢 8 条）。本项目把验证升级成**读确认挑战-应答**：一方写文件→另一方必须**读到文件内容**才能正确回应→反向同理。任一环节断 = 该回合**硬 FAIL**，无处藏。

---

## 这是什么（一句话）

- **center**：中心机跑一个零依赖（python3 stdlib）HTTP 服务，登记节点 + 充当握手对端 + 聚合每节点的 ok/fail。
- **node**：节点机跑一个零依赖握手 agent，**只需对 center 的出站 HTTP**（不需要 center 能 ssh 进来，所以 NAT/无公钥的机器也能装，如 Mac）。
- **握手**：每回合两个方向都用 nonce **读确认**（见 [docs/PROTOCOL.md](docs/PROTOCOL.md)）。

## 为什么用 HTTP（node→center 出站）而不是 ssh

ssh 文件握手要求 center 能反向连进 node（双向公钥）。很多节点做不到（Mac 无 najol 公钥、NAT 后、防火墙）。**node→center 出站 HTTP** 是最低门槛：只要节点能访问 center 的端口就行（它本来就要上报）。读确认语义不变——“文件”是必须被读到内容才能正确应答的那份 nonce 载荷。

---

## 快速开始

### 1) 装 center（在中心机，如 najol）
```bash
curl -fsSL https://raw.githubusercontent.com/GeojoL/machine-nodes/main/center-installer.sh | bash
# 或克隆后:  ./center-installer.sh
```
默认起在 `:8770`（可 `CENTER_PORT=xxxx` 改）。装完 `curl http://<center>:8770/hs/status` 应回 JSON。

### 2) 装 node（在任意节点机，如 ken 的 Mac / 树莓派）
```bash
curl -fsSL https://raw.githubusercontent.com/GeojoL/machine-nodes/main/nodes-installer.sh | CENTER=http://<center-ip>:8770 bash
```
装完：自动注册、起一个**随机命名的人格**、开始握手循环、把 ok/fail 写本地 log + 上报 center。

### 3) 看健康（中心机或任意能访问 center 的机器）
```bash
curl -s http://<center>:8770/hs/status | python3 -m json.tool   # 每节点 ok/fail/last-seen
```

---

## 仓库结构 / 完整引索

| 路径 | 作用 |
|------|------|
| `README.md` | 本文（总览 + 引索 + 快速开始） |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | 握手协议规范（nonce 读确认、回合时序、失败语义） |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | center/node 组件、数据流、端口、目录布局 |
| [`docs/INSTALL.md`](docs/INSTALL.md) | 详细安装（center/node）、参数、卸载、升级 |
| [`docs/PERSONA.md`](docs/PERSONA.md) | 节点人格（随机命名规则、唯一性、生命周期） |
| [`docs/SOAK.md`](docs/SOAK.md) | 12h 文件握手压力测：起停、判读、对账、real ok/fail |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | 常见故障（连不上/丢包/时钟/权限）+ 定位 |
| `center/center-agent.py` | 中心 HTTP 握手服务（零依赖 python3） |
| `center/center.service.tmpl` | center 的 systemd unit 模板 |
| `node/node-agent.py` | 节点握手 agent（零依赖 python3，出站 HTTP） |
| `node/node.service.tmpl` | node 的 systemd unit 模板（Linux） |
| `node/node.plist.tmpl` | node 的 launchd plist 模板（macOS） |
| `center-installer.sh` | 一键装 center |
| `nodes-installer.sh` | 一键装 node（含随机人格 + 加入握手） |

## 设计原则

1. **零依赖**：只用 python3 stdlib + bash。任何 Mac/Linux/Pi 开箱即装，不碰 npm/pip。
2. **不写死**：center 地址、端口、节点名、人格名全走参数/env/探活，绝不硬编码。
3. **读确认 > 心跳**：健康只认 nonce 双向对上的 ok/fail，丢包硬 FAIL 入 log。
4. **幂等 + 可回滚**：installer 可重复跑；带卸载；改前备份。
5. **出站优先**：node 只需出站 HTTP，最大化可部署性。

## 状态

v0 — najol 自建自测中。先在树莓派（rasjol）+ ken 的 Mac 上验，跑 12h 握手。
