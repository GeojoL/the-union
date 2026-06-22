# 节点通讯身份 · 防冒充(ccp-resolve-id)

源起:多 AI 共享同一 unix 账号 + Claude 每条 bash 调用环境变量不持久 → `ccp-send`/`ccp-pull` 若用 `${CCP_ID:-<某默认身份>}` 静默兜底,任何没逐条带 `CCP_ID` 的 agent 都会**冒充那个默认身份**。Mahaul@macjol 实战踩过,najol 定 B+ 标准,本 toolkit 落地全节点同律。

## 机制(`node/ccp-resolve-id`,装到 `~/.local/bin/`)
解析当前 CCP 身份,供 `ccp-send`/`ccp-pull` 复用(DRY 单点):
1. `$CCP_ID` 已设 → 直接用(显式优先)。
2. 未设 → 从 **tmux 窗口名**派生:`capitalize(window_name) + '@' + <节点名>`。
   - 节点名读 `~/.ccp-node`(installer 写入 = `$NODE`)。**不裸用 hostname**——某些机器 hostname ≠ 节点名(如 macjol 的 hostname 是 GeojoLus-MBP)。
3. 派生后对 **`~/.ccp-inbox/roster`**(本机通讯参与者名单)校验——不在名单 → 拒。
4. 拿不到窗口名(无 `TMUX_PANE`/不在 tmux/名空)或无 roster → **fail-loud 拒发(exit 非 0),绝不回退硬编码身份**。这是杀冒充的核心。

## 安装产物(`nodes-installer.sh` 自动装)
- `~/.local/bin/ccp-resolve-id` — 解析器。
- `~/.ccp-node` — 节点名(= 安装时的 `$NODE`,各机自己的)。
- `~/.ccp-inbox/roster` — 本机通讯参与者名单脚手架,每行 `Capitalize(窗口名)@<节点名>`。按本机窗口名填。

## roster 维护
- **节点本地维护**为校验源(每机 `~/.ccp-inbox/roster`)。
- **正式 AI 身份权威在根 `AI-PROTOCOL §1`**:新增/改名正式 AI 先 `core-report` 报根、由根改 §1 并广播。
- dev persona(节点本地的研发/工具 AI)留本地 roster 即可,不必进 §1。

## ⚠ 接线 caveat(必读)
把 `ccp-send`/`ccp-pull` 默认改成 fail-loud 后,**非 tmux 且没设 `CCP_ID` 的 launchd/cron 调用者会被拒**(如投递门铃内部调 `ccp-pull`)。给这类自动调用者在其 plist/systemd unit 环境里**显式设 `CCP_ID`**,否则通讯故障。安装/接线前自查一遍所有自动调用者。

## 验证(DRY)
- 从某 AI 窗口未设 `CCP_ID` 调用 → 派生出该窗对应身份(非默认身份)。
- 非 tmux / 派生身份不在 roster → fail-loud 拒(exit 3 / 4)。
