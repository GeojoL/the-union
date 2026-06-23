// union-tui — The Union 中心机/节点机控制台 TUI(v1.1)。
// 面板:Messages(消息中心,live刷新) / Personas(列表+e调$EDITOR编辑) / Services(守护/门铃/心跳 状态+控制) / Settings(机群/节点/center地址,e编辑).
// 无头自检:`union-tui --dump`。数据源:~/.ccp-inbox/{inbox,local}.jsonl + $AGENTS_ROOT/*/PERSONA.md + ~/.ccp-{node,cluster,center} + launchctl。
// 后续:LAN信标发送、center模式(najol聚合全节点)、加人格写入表单。

use std::fs;
use std::io;
use std::path::PathBuf;
use std::process::Command;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Tabs, Wrap};
use ratatui::Frame;

#[derive(Clone)]
struct Msg { ts: String, from: String, to: String, kind: String, body: String, via: &'static str }
#[derive(Clone)]
struct Persona { name: String, identity: String, level: String, role: String, window: String, home: String }
#[derive(Clone)]
struct Service { label: String, desc: String, loaded: bool, pid: String, last_exit: String }

fn home() -> PathBuf { dirs::home_dir().unwrap_or_else(|| PathBuf::from(".")) }
fn agents_root() -> PathBuf {
    std::env::var("AGENTS_ROOT").map(PathBuf::from).unwrap_or_else(|_| home().join("mahaul/agents"))
}
fn read_kv(file: &str, key: &str) -> String {
    if let Ok(c) = fs::read_to_string(home().join(file)) {
        for l in c.lines() {
            if let Some(r) = l.strip_prefix(&format!("{}=", key)) { return r.trim().to_string(); }
        }
    }
    String::new()
}
fn node_name() -> String {
    let n = fs::read_to_string(home().join(".ccp-node")).map(|s| s.trim().to_string()).unwrap_or_default();
    if n.is_empty() { "?".into() } else { n }
}

fn read_jsonl(path: PathBuf, via: &'static str) -> Vec<Msg> {
    let mut out = Vec::new();
    if let Ok(content) = fs::read_to_string(&path) {
        for line in content.lines() {
            let line = line.trim();
            if line.is_empty() { continue; }
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                let g = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
                let mut body = g("body");
                if body.is_empty() {
                    if let Some(a) = v.get("ack").and_then(|x| x.as_str()) { body = format!("(ack {})", a); }
                }
                out.push(Msg { ts: g("ts"), from: g("from"), to: g("to"), kind: g("kind"), body, via });
            }
        }
    }
    out
}
fn read_bus() -> Vec<Msg> {
    let base = home().join(".ccp-inbox");
    let mut m = read_jsonl(base.join("inbox.jsonl"), "najol");
    m.extend(read_jsonl(base.join("local.jsonl"), "local"));
    m.sort_by(|a, b| a.ts.cmp(&b.ts));
    m
}

fn parse_persona(path: &PathBuf) -> Option<Persona> {
    let c = fs::read_to_string(path).ok()?;
    let f = |k: &str| -> String {
        for l in c.lines() {
            if let Some(r) = l.strip_prefix(&format!("{}:", k)) { return r.trim().to_string(); }
        }
        String::new()
    };
    let name = f("name");
    if name.is_empty() { return None; }
    Some(Persona { name, identity: f("identity"), level: f("level"), role: f("role"), window: f("window"), home: f("home") })
}
fn read_personas() -> Vec<Persona> {
    let mut out = Vec::new();
    if let Ok(es) = fs::read_dir(agents_root()) {
        for e in es.flatten() {
            let pf = e.path().join("PERSONA.md");
            if pf.is_file() { if let Some(p) = parse_persona(&pf) { out.push(p); } }
        }
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out
}

// 已知本机服务(launchd label -> 说明)
const SERVICES: &[(&str, &str)] = &[
    ("com.geojol.agents-guard", "守护:死了拉起/非bypass空闲重启/180s"),
    ("com.geojol.agents-doorbell", "门铃:给各agent投递消息/60s"),
    ("com.geojol.ccp-heartbeat", "心跳:活性信标/60s"),
    ("com.geojol.ccp-doorbell", "我的门铃:najol拉取+投递/60s"),
];
fn read_services() -> Vec<Service> {
    SERVICES.iter().map(|(label, desc)| {
        let out = Command::new("launchctl").arg("list").arg(label).output();
        let (loaded, pid, last_exit) = match out {
            Ok(o) if o.status.success() => {
                let s = String::from_utf8_lossy(&o.stdout);
                let grab = |k: &str| s.lines().find(|l| l.contains(k))
                    .and_then(|l| l.split('=').nth(1)).map(|v| v.trim().trim_end_matches(';').trim().to_string()).unwrap_or_default();
                (true, grab("\"PID\""), grab("\"LastExitStatus\""))
            }
            _ => (false, String::new(), String::new()),
        };
        Service { label: label.to_string(), desc: desc.to_string(), loaded, pid, last_exit }
    }).collect()
}

// ---- center HTTP(极简 TcpStream 客户端,不引重依赖,守单二进制跨平台)----
fn center_base() -> String {
    let c = read_kv(".ccp-center", "center");
    if c.is_empty() { "http://10.68.63.93:8770".into() } else { c.trim_end_matches('/').to_string() }
}
fn parse_url(url: &str) -> Option<(String, u16, String)> {
    let rest = url.strip_prefix("http://")?;
    let (hp, path) = match rest.find('/') { Some(i) => (&rest[..i], &rest[i..]), None => (rest, "/") };
    let (host, port) = match hp.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse().unwrap_or(80)), None => (hp.to_string(), 80) };
    Some((host, port, path.to_string()))
}
fn http_req(method: &str, url: &str, body: Option<&str>) -> io::Result<String> {
    use std::io::{Read, Write};
    use std::net::TcpStream;
    let (host, port, path) = parse_url(url).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "bad url"))?;
    let mut s = TcpStream::connect((host.as_str(), port))?;
    s.set_read_timeout(Some(Duration::from_secs(4))).ok();
    s.set_write_timeout(Some(Duration::from_secs(4))).ok();
    let b = body.unwrap_or("");
    let req = format!("{} {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
        method, path, host, b.len(), b);
    s.write_all(req.as_bytes())?;
    let mut raw = String::new();
    s.read_to_string(&mut raw)?;
    Ok(raw.splitn(2, "\r\n\r\n").nth(1).unwrap_or("").to_string())
}
fn http_get(url: &str) -> io::Result<String> { http_req("GET", url, None) }
fn http_post(url: &str, body: &str) -> io::Result<String> { http_req("POST", url, Some(body)) }

#[derive(Clone, Default)]
struct Addr { addr: String, kind: String, source: String, probe: String }
#[derive(Clone, Default)]
struct Node {
    name: String, persona: String, machine: String, online: bool, online_reason: String,
    fp: String, weak_fp: bool, conflicts: i64, cluster_match: bool, last_seen: String, rtt: i64, addrs: Vec<Addr>,
}
#[derive(Clone, Default)]
struct Discovered { node: String, addr: String, src_addr: String, addr_matches_src: bool, count: i64 }
struct Cluster { cluster_id: String, name: String, schema: i64, online_ttl: i64, nodes: Vec<Node>, err: String }

fn jstr(v: &serde_json::Value, k: &str) -> String { v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string() }
fn jbool(v: &serde_json::Value, k: &str) -> bool { v.get(k).and_then(|x| x.as_bool()).unwrap_or(false) }
fn jint(v: &serde_json::Value, k: &str) -> i64 { v.get(k).and_then(|x| x.as_i64()).unwrap_or(0) }

fn fetch_cluster() -> Cluster {
    let url = format!("{}/cluster", center_base());
    let mut c = Cluster { cluster_id: String::new(), name: String::new(), schema: 0, online_ttl: 0, nodes: vec![], err: String::new() };
    match http_get(&url) {
        Err(e) => c.err = format!("连不上 center {}: {}", url, e),
        Ok(txt) => match serde_json::from_str::<serde_json::Value>(&txt) {
            Err(e) => c.err = format!("解析失败: {} (原文 {}B)", e, txt.len()),
            Ok(v) => {
                c.cluster_id = jstr(&v, "cluster_id"); c.name = jstr(&v, "cluster_name");
                c.schema = jint(&v, "schema"); c.online_ttl = jint(&v, "online_ttl_s");
                if let Some(nodes) = v.get("nodes").and_then(|x| x.as_object()) {
                    for (k, nv) in nodes {
                        let mut n = Node {
                            name: { let s = jstr(nv, "node"); if s.is_empty() { k.clone() } else { s } },
                            persona: jstr(nv, "persona"), machine: jstr(nv, "machine"),
                            online: jbool(nv, "online"), online_reason: jstr(nv, "online_reason"),
                            fp: jstr(nv, "fp"), weak_fp: jbool(nv, "weak_fp"), conflicts: jint(nv, "conflicts"),
                            cluster_match: jbool(nv, "cluster_match"), last_seen: jstr(nv, "last_seen"),
                            rtt: jint(nv, "last_rtt_ms"), addrs: vec![],
                        };
                        if let Some(arr) = nv.get("addresses").and_then(|x| x.as_array()) {
                            for a in arr { n.addrs.push(Addr { addr: jstr(a, "addr"), kind: jstr(a, "kind"), source: jstr(a, "source"), probe: jstr(a, "probe_state") }); }
                        }
                        c.nodes.push(n);
                    }
                    c.nodes.sort_by(|a, b| a.name.cmp(&b.name));
                }
            }
        },
    }
    c
}
fn fetch_discovered() -> Vec<Discovered> {
    let url = format!("{}/discovered", center_base());
    let mut out = vec![];
    if let Ok(txt) = http_get(&url) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(&txt) {
            if let Some(arr) = v.get("nodes").and_then(|x| x.as_array()) {
                for d in arr { out.push(Discovered { node: jstr(d, "node"), addr: jstr(d, "addr"), src_addr: jstr(d, "src_addr"), addr_matches_src: jbool(d, "addr_matches_src"), count: jint(d, "count") }); }
            }
        }
    }
    out
}
// confirm/reject:授信路径,打 center POST /discovered/{confirm,reject}
fn discovered_action(act: &str, node: &str) -> io::Result<String> {
    http_post(&format!("{}/discovered/{}", center_base(), act), &format!("{{\"node\":\"{}\"}}", node))
}

#[derive(PartialEq, Clone, Copy)]
enum Tab { Messages, Personas, Services, Cluster, Settings }

struct App {
    tab: Tab,
    msgs: Vec<Msg>,
    personas: Vec<Persona>,
    services: Vec<Service>,
    cluster: Cluster,
    discovered: Vec<Discovered>,
    msg_state: ListState,
    persona_state: ListState,
    service_state: ListState,
    node_state: ListState,
    disc_state: ListState,
    only_to_me: bool,
    follow: bool,
    status: String,
    quit: bool,
    last_refresh: Instant,
    last_cluster: Instant,
}
impl App {
    fn new() -> Self {
        let mut a = App {
            tab: Tab::Messages, msgs: read_bus(), personas: read_personas(), services: read_services(),
            cluster: Cluster { cluster_id: String::new(), name: String::new(), schema: 0, online_ttl: 0, nodes: vec![], err: "(进 Cluster 标签或按 r 拉取)".into() },
            discovered: vec![],
            msg_state: ListState::default(), persona_state: ListState::default(), service_state: ListState::default(),
            node_state: ListState::default(), disc_state: ListState::default(),
            only_to_me: false, follow: true, status: String::new(), quit: false, last_refresh: Instant::now(), last_cluster: Instant::now(),
        };
        let n = a.filtered_msgs().len();
        if n > 0 { a.msg_state.select(Some(n - 1)); }
        if !a.personas.is_empty() { a.persona_state.select(Some(0)); }
        if !a.services.is_empty() { a.service_state.select(Some(0)); }
        a
    }
    fn refresh_cluster(&mut self) {
        self.cluster = fetch_cluster();
        self.discovered = fetch_discovered();
        if !self.cluster.nodes.is_empty() && self.node_state.selected().is_none() { self.node_state.select(Some(0)); }
        if !self.discovered.is_empty() && self.disc_state.selected().is_none() { self.disc_state.select(Some(0)); }
        self.last_cluster = Instant::now();
    }
    fn me(&self) -> String { format!("Mahaul@{}", node_name()) }
    fn filtered_msgs(&self) -> Vec<Msg> {
        let me = self.me();
        self.msgs.iter().filter(|m| !self.only_to_me || m.to == me || m.to == "broadcast").cloned().collect()
    }
    fn refresh_bus(&mut self) {
        let at_bottom = {
            let n = self.filtered_msgs().len();
            self.msg_state.selected().map(|i| i + 1 >= n).unwrap_or(true)
        };
        self.msgs = read_bus();
        let n = self.filtered_msgs().len();
        if self.follow && at_bottom && n > 0 { self.msg_state.select(Some(n - 1)); }
        self.last_refresh = Instant::now();
    }
    fn reload_all(&mut self) {
        self.msgs = read_bus(); self.personas = read_personas(); self.services = read_services();
        self.status = "已刷新".into();
    }
    fn next(&mut self) {
        match self.tab {
            Tab::Messages => { let n = self.filtered_msgs().len(); step(&mut self.msg_state, n, 1); }
            Tab::Personas => step(&mut self.persona_state, self.personas.len(), 1),
            Tab::Services => step(&mut self.service_state, self.services.len(), 1),
            Tab::Cluster => step(&mut self.node_state, self.cluster.nodes.len(), 1),
            Tab::Settings => {}
        }
    }
    fn prev(&mut self) {
        match self.tab {
            Tab::Messages => { let n = self.filtered_msgs().len(); step(&mut self.msg_state, n, -1); }
            Tab::Personas => step(&mut self.persona_state, self.personas.len(), -1),
            Tab::Services => step(&mut self.service_state, self.services.len(), -1),
            Tab::Cluster => step(&mut self.node_state, self.cluster.nodes.len(), -1),
            Tab::Settings => {}
        }
    }
}
fn step(state: &mut ListState, len: usize, dir: i32) {
    if len == 0 { return; }
    let cur = state.selected().unwrap_or(0) as i32;
    state.select(Some(((cur + dir).rem_euclid(len as i32)) as usize));
}

fn ensure_center_config() -> PathBuf {
    let p = home().join(".ccp-center");
    if !p.exists() {
        let _ = fs::write(&p, "# The Union 中心机地址(手填/可改)。center 优先,fallback 兜底。\ncenter=http://10.68.63.93:8770\ncenter_fallback=http://192.168.50.50:8770\n");
    }
    p
}

fn dump() {
    let msgs = read_bus(); let personas = read_personas(); let services = read_services();
    ensure_center_config();
    println!("=== union-tui --dump (node={}) ===", node_name());
    println!("\n[机群] cluster_id={} name={}", read_kv(".ccp-cluster", "cluster_id"), read_kv(".ccp-cluster", "cluster_name"));
    println!("[center] {} (fallback {})", read_kv(".ccp-center", "center"), read_kv(".ccp-center", "center_fallback"));
    println!("\n[消息] {} 条,末 5:", msgs.len());
    for m in msgs.iter().rev().take(5).rev() {
        let b: String = m.body.lines().next().unwrap_or("").chars().take(60).collect();
        println!("  [{}|{}] {}->{} ({}): {}", m.via, &m.ts.chars().take(16).collect::<String>(), m.from, m.to, m.kind, b);
    }
    println!("\n[人格] {}:", personas.len());
    for p in &personas { println!("  {} [{}] {} | {}", p.identity, p.level, p.window, p.role); }
    println!("\n[服务] {}:", services.len());
    for s in &services {
        println!("  {} loaded={} pid={} lastExit={} | {}", s.label, s.loaded, if s.pid.is_empty(){"-"}else{&s.pid}, if s.last_exit.is_empty(){"-"}else{&s.last_exit}, s.desc);
    }
    // center 模式数据(实拉 /cluster + /discovered)
    let cl = fetch_cluster(); let disc = fetch_discovered();
    if cl.err.is_empty() {
        println!("\n[机群·center {}] cluster_id={} name={} schema={} 节点 {}:", center_base(), cl.cluster_id, cl.name, cl.schema, cl.nodes.len());
        for n in &cl.nodes {
            let a: Vec<String> = n.addrs.iter().map(|x| format!("{}({})", x.addr, x.kind)).collect();
            println!("  {} {} [{}] fp={}{} conflicts={} addrs=[{}]",
                if n.online {"●"} else {"○"}, n.name, n.online_reason, short(&n.fp,12),
                if n.weak_fp {" weak"} else {""}, n.conflicts, a.join(","));
        }
        println!("[发现待确认] {}:", disc.len());
        for d in &disc { println!("  {} {} src{} x{}", d.node, d.addr, if d.addr_matches_src {"✓"} else {"✗"}, d.count); }
    } else {
        println!("\n[机群·center {}] 拉取失败: {}", center_base(), cl.err);
    }
}

fn short(s: &str, n: usize) -> String { if s.chars().count() > n { s.chars().take(n).collect() } else { s.to_string() } }

fn ui(f: &mut Frame, app: &mut App) {
    let c = Layout::vertical([Constraint::Length(3), Constraint::Min(0), Constraint::Length(1)]).split(f.area());
    let titles = vec!["Messages", "Personas", "Services", "Cluster", "Settings"];
    let sel = match app.tab { Tab::Messages=>0, Tab::Personas=>1, Tab::Services=>2, Tab::Cluster=>3, Tab::Settings=>4 };
    f.render_widget(
        Tabs::new(titles).select(sel)
            .block(Block::default().borders(Borders::ALL).title(format!(" The Union · {} · {} ", node_name(), read_kv(".ccp-cluster","cluster_name"))))
            .highlight_style(Style::default().fg(Color::Black).bg(Color::Cyan)),
        c[0]);
    match app.tab {
        Tab::Messages => render_messages(f, app, c[1]),
        Tab::Personas => render_personas(f, app, c[1]),
        Tab::Services => render_services(f, app, c[1]),
        Tab::Cluster => render_cluster(f, app, c[1]),
        Tab::Settings => render_settings(f, app, c[1]),
    }
    let help = match app.tab {
        Tab::Messages => " q退出 Tab切 ↑↓滚 f仅发我的 r刷新 (live自动刷新) ",
        Tab::Personas => " q退出 Tab切 ↑↓选 e用$EDITOR编辑人格 r刷新 ",
        Tab::Services => " q退出 Tab切 ↑↓选 k立即跑(kickstart) u跑版本升级 r刷新 ",
        Tab::Cluster => " q退出 Tab切 ↑↓选节点 c确认发现 x拒绝发现 r刷新(每10s自动) ",
        Tab::Settings => " q退出 Tab切 e编辑中心地址(~/.ccp-center,手填) r刷新 ",
    };
    let st = if app.status.is_empty() { help.to_string() } else { format!("{} | {}", help, app.status) };
    f.render_widget(Paragraph::new(st).style(Style::default().fg(Color::DarkGray)), c[2]);
}

fn render_messages(f: &mut Frame, app: &mut App, area: Rect) {
    let cols = Layout::horizontal([Constraint::Percentage(50), Constraint::Percentage(50)]).split(area);
    let fm = app.filtered_msgs();
    let items: Vec<ListItem> = fm.iter().map(|m| {
        let ts: String = m.ts.chars().skip(5).take(11).collect();
        let color = if m.via == "local" { Color::Green } else { Color::Yellow };
        ListItem::new(Line::from(vec![
            Span::styled(format!("{:11} ", ts), Style::default().fg(Color::DarkGray)),
            Span::styled(format!("{:<16}", short(&m.from, 16)), Style::default().fg(color)),
            Span::raw(format!("→{}", short(&m.to, 13))),
        ]))
    }).collect();
    let title = format!(" 消息 {}{} ", fm.len(), if app.only_to_me { " (仅发我)" } else { "" });
    f.render_stateful_widget(
        List::new(items).block(Block::default().borders(Borders::ALL).title(title))
            .highlight_style(Style::default().add_modifier(Modifier::REVERSED)),
        cols[0], &mut app.msg_state);
    let detail = app.msg_state.selected().and_then(|i| fm.get(i))
        .map(|m| format!("时间: {}\n来源: {} ({})\n收件: {}\n类型: {}\n\n{}", m.ts, m.from, m.via, m.to, m.kind, m.body))
        .unwrap_or_else(|| "(无消息)".into());
    f.render_widget(Paragraph::new(detail).wrap(Wrap { trim: false })
        .block(Block::default().borders(Borders::ALL).title(" 详情 ")), cols[1]);
}

fn render_personas(f: &mut Frame, app: &mut App, area: Rect) {
    let cols = Layout::horizontal([Constraint::Percentage(45), Constraint::Percentage(55)]).split(area);
    let items: Vec<ListItem> = app.personas.iter().map(|p| ListItem::new(Line::from(vec![
        Span::styled(format!("{:<18}", p.identity), Style::default().fg(Color::Cyan)),
        Span::styled(format!("[{}]", p.level), Style::default().fg(Color::Magenta)),
    ]))).collect();
    f.render_stateful_widget(
        List::new(items).block(Block::default().borders(Borders::ALL).title(format!(" 人格 {} ", app.personas.len())))
            .highlight_style(Style::default().add_modifier(Modifier::REVERSED)),
        cols[0], &mut app.persona_state);
    let detail = app.persona_state.selected().and_then(|i| app.personas.get(i))
        .map(|p| format!("身份: {}\n级别: {}\n窗口: {}\n家目录: {}\n\n职责:\n{}\n\n(e 用 $EDITOR/nvim 编辑 PERSONA.md)", p.identity, p.level, p.window, p.home, p.role))
        .unwrap_or_else(|| "(无人格;add-persona 添加)".into());
    f.render_widget(Paragraph::new(detail).wrap(Wrap { trim: false })
        .block(Block::default().borders(Borders::ALL).title(" 详情 ")), cols[1]);
}

fn render_services(f: &mut Frame, app: &mut App, area: Rect) {
    let items: Vec<ListItem> = app.services.iter().map(|s| {
        let (mark, color) = if s.loaded { ("●", Color::Green) } else { ("○", Color::Red) };
        ListItem::new(Line::from(vec![
            Span::styled(format!("{} ", mark), Style::default().fg(color)),
            Span::styled(format!("{:<28}", s.label.replace("com.geojol.", "")), Style::default().fg(Color::Cyan)),
            Span::raw(format!("pid {:<7} exit {:<4} ", if s.pid.is_empty(){"-"}else{&s.pid}, if s.last_exit.is_empty(){"-"}else{&s.last_exit})),
            Span::styled(s.desc.clone(), Style::default().fg(Color::DarkGray)),
        ]))
    }).collect();
    f.render_stateful_widget(
        List::new(items).block(Block::default().borders(Borders::ALL)
            .title(" 服务(k=立即跑 kickstart / u=跑版本升级 agent-update) "))
            .highlight_style(Style::default().add_modifier(Modifier::REVERSED)),
        area, &mut app.service_state);
}

fn render_cluster(f: &mut Frame, app: &mut App, area: Rect) {
    let disc_h = (app.discovered.len() as u16 + 3).clamp(3, 9);
    let rows = Layout::vertical([Constraint::Min(0), Constraint::Length(disc_h)]).split(area);
    let cols = Layout::horizontal([Constraint::Percentage(52), Constraint::Percentage(48)]).split(rows[0]);
    // 节点表
    let items: Vec<ListItem> = app.cluster.nodes.iter().map(|n| {
        let (mark, color) = if n.online { ("●", Color::Green) } else { ("○", Color::DarkGray) };
        let mut z = false; let mut l = false;
        for a in &n.addrs { if a.kind == "zt" { z = true } else if a.kind == "lan" { l = true } }
        let kinds = format!("{}{}", if z {"ZT"} else {"  "}, if l {"/LAN"} else {""});
        ListItem::new(Line::from(vec![
            Span::styled(format!("{} ", mark), Style::default().fg(color)),
            Span::styled(format!("{:<10}", short(&n.name, 10)), Style::default().fg(Color::Cyan)),
            Span::raw(format!("{:<12}", short(&n.online_reason, 12))),
            Span::styled(format!("{:<6}", kinds), Style::default().fg(Color::Blue)),
            Span::styled(if n.conflicts > 0 { format!("⚠{}", n.conflicts) } else { String::new() }, Style::default().fg(Color::Red)),
        ]))
    }).collect();
    let title = if app.cluster.err.is_empty() {
        format!(" 节点 {} · {} · schema{} ", app.cluster.nodes.len(), short(&app.cluster.name, 16), app.cluster.schema)
    } else { " 节点(未拉到/出错,见详情) ".to_string() };
    f.render_stateful_widget(List::new(items).block(Block::default().borders(Borders::ALL).title(title))
        .highlight_style(Style::default().add_modifier(Modifier::REVERSED)), cols[0], &mut app.node_state);
    // 详情
    let detail = if !app.cluster.err.is_empty() && app.cluster.nodes.is_empty() {
        format!("center: {}\ncluster_id: {}\n\n{}", center_base(), app.cluster.cluster_id, app.cluster.err)
    } else {
        app.node_state.selected().and_then(|i| app.cluster.nodes.get(i)).map(|n| {
            let addrs = if n.addrs.is_empty() { "  (无)".to_string() } else {
                n.addrs.iter().map(|a| format!("  {} [{}] src={} probe={}", a.addr, a.kind, a.source, a.probe)).collect::<Vec<_>>().join("\n") };
            format!("节点: {}\n身份: {}\n机型: {}\n在线: {} ({})\nfp: {}{}\ncluster_match: {}   conflicts: {}\nlast_seen: {}   rtt: {}ms\n\n地址:\n{}",
                n.name, n.persona, n.machine, if n.online {"●在线"} else {"○离线"}, n.online_reason,
                short(&n.fp, 16), if n.weak_fp {" (weak_fp)"} else {""}, n.cluster_match, n.conflicts, n.last_seen, n.rtt, addrs)
        }).unwrap_or_else(|| "(无节点)".into())
    };
    f.render_widget(Paragraph::new(detail).wrap(Wrap { trim: false })
        .block(Block::default().borders(Borders::ALL).title(format!(" 详情 (center {}) ", center_base()))), cols[1]);
    // LAN 发现待确认
    let ditems: Vec<ListItem> = app.discovered.iter().map(|d| ListItem::new(Line::from(vec![
        Span::styled(format!("{:<10}", short(&d.node, 10)), Style::default().fg(Color::Yellow)),
        Span::raw(format!("{:<16}", short(&d.addr, 16))),
        Span::styled(if d.addr_matches_src { "src✓".to_string() } else { format!("src✗({})", d.src_addr) },
            Style::default().fg(if d.addr_matches_src { Color::Green } else { Color::Red })),
        Span::raw(format!(" x{}", d.count)),
    ]))).collect();
    f.render_stateful_widget(List::new(ditems).block(Block::default().borders(Borders::ALL)
        .title(format!(" LAN 发现待确认 {} (c确认入群 / x拒绝) ", app.discovered.len())))
        .highlight_style(Style::default().add_modifier(Modifier::REVERSED)), rows[1], &mut app.disc_state);
}

fn render_settings(f: &mut Frame, app: &App, area: Rect) {
    ensure_center_config();
    let me = app.me();
    let to_me = app.msgs.iter().filter(|m| m.to == me).count();
    let text = format!(
        "节点(~/.ccp-node): {}\n本机身份: {}\n机群(~/.ccp-cluster): id={} name={}\n中心地址(~/.ccp-center): {}\n  fallback: {}\n\n人格: {}   服务: {}\n总线消息: {} (najol {} + local {})  发给我: {}\n\n[手填地址] 按 e 用 $EDITOR/nvim 编辑 ~/.ccp-center(中心+其它地址)。\n[机群识别] cluster_id 自动取自 ZeroTier 网络;LAN 内节点经 UDP 信标自动发现(center 侧 najol 做中)。\n[多地址] 一台机器多地址(ZT+各LAN段)按节点名归并、多路 failover(center 侧做中)。",
        node_name(), me,
        read_kv(".ccp-cluster", "cluster_id"), read_kv(".ccp-cluster", "cluster_name"),
        read_kv(".ccp-center", "center"), read_kv(".ccp-center", "center_fallback"),
        app.personas.len(), app.services.len(),
        app.msgs.len(), app.msgs.iter().filter(|m| m.via=="najol").count(), app.msgs.iter().filter(|m| m.via=="local").count(), to_me,
    );
    f.render_widget(Paragraph::new(text).wrap(Wrap { trim: false })
        .block(Block::default().borders(Borders::ALL).title(" 设置 / 状态 ")), area);
}

fn suspend_run(cmd: &str, args: &[&str], reinit: bool) -> Option<ratatui::DefaultTerminal> {
    ratatui::restore();
    let _ = Command::new(cmd).args(args).status();
    if reinit { Some(ratatui::init()) } else { None }
}

// 多地址自收集(跨平台,不 shell ifconfig):枚举本机非 loopback IPv4。
fn collect_addrs() -> Vec<String> {
    let mut v = Vec::new();
    if let Ok(ifaces) = if_addrs::get_if_addrs() {
        for i in ifaces {
            if i.is_loopback() { continue; }
            let ip = i.ip();
            if ip.is_ipv4() { v.push(ip.to_string()); }
        }
    }
    v.sort(); v.dedup(); v
}

// LAN 组播信标:广播 {cluster_id,node,role,addrs[],port,ts} → center 监听自动发现(只收同 cluster_id)。
fn beacon(once: bool) -> io::Result<()> {
    use std::net::UdpSocket;
    let (group, port) = ("239.255.63.70", 48770u16);
    let cluster = read_kv(".ccp-cluster", "cluster_id");
    let node = node_name();
    let role = { let r = read_kv(".ccp-center", "role"); if r == "center" { "center" } else { "node" } };
    let sock = UdpSocket::bind("0.0.0.0:0")?;
    sock.set_multicast_ttl_v4(1).ok();
    let dest = format!("{}:{}", group, port);
    loop {
        let addrs = collect_addrs();
        let ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        let payload = serde_json::json!({
            "cluster_id": cluster, "node": node, "role": role, "addrs": addrs, "port": port, "ts": ts
        }).to_string();
        let sent = sock.send_to(payload.as_bytes(), &dest);
        if once { println!("beacon → {} : {}\n(发送结果: {:?})", dest, payload, sent.map(|n| format!("{}B", n))); return Ok(()); }
        std::thread::sleep(Duration::from_secs(15));
    }
}

fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("--dump") => { dump(); return Ok(()); }
        Some("beacon") => { return beacon(args.iter().any(|a| a == "--once")); }
        Some("-h") | Some("--help") => {
            println!("union-tui [tui|beacon|--dump]\n  (无参/tui)交互控制台  beacon[ --once]LAN信标  --dump无头自检");
            return Ok(());
        }
        _ => {}
    }
    ensure_center_config();
    let mut terminal = ratatui::init();
    let mut app = App::new();
    while !app.quit {
        terminal.draw(|f| ui(f, &mut app))?;
        // live 刷新:每 ~3s 重读总线(低 CPU)
        if app.last_refresh.elapsed() >= Duration::from_secs(3) { app.refresh_bus(); }
        if app.tab == Tab::Cluster && app.last_cluster.elapsed() >= Duration::from_secs(10) { app.refresh_cluster(); }
        if event::poll(Duration::from_millis(500))? {
            if let Event::Key(k) = event::read()? {
                if k.kind != KeyEventKind::Press { continue; }
                app.status.clear();
                match k.code {
                    KeyCode::Char('q') | KeyCode::Esc => app.quit = true,
                    KeyCode::Tab => {
                        app.tab = match app.tab {
                            Tab::Messages=>Tab::Personas, Tab::Personas=>Tab::Services, Tab::Services=>Tab::Cluster,
                            Tab::Cluster=>Tab::Settings, Tab::Settings=>Tab::Messages };
                        if app.tab == Tab::Cluster && app.cluster.nodes.is_empty() { app.refresh_cluster(); }
                    }
                    KeyCode::Down | KeyCode::Char('j') => app.next(),
                    KeyCode::Up | KeyCode::Char('k') if app.tab != Tab::Services => app.prev(),
                    KeyCode::Char('f') if app.tab == Tab::Messages => {
                        app.only_to_me = !app.only_to_me;
                        let n = app.filtered_msgs().len();
                        app.msg_state.select(if n > 0 { Some(n-1) } else { None });
                    }
                    KeyCode::Char('r') if app.tab == Tab::Cluster => { app.refresh_cluster(); app.status = "已拉取 /cluster + /discovered".into(); }
                    KeyCode::Char('r') => app.reload_all(),
                    KeyCode::Char('c') if app.tab == Tab::Cluster => {
                        if let Some(d) = app.disc_state.selected().and_then(|i| app.discovered.get(i)).cloned() {
                            match discovered_action("confirm", &d.node) {
                                Ok(_) => app.status = format!("已确认 {} 入群", d.node),
                                Err(e) => app.status = format!("确认失败: {}", e),
                            }
                            app.refresh_cluster();
                        } else { app.status = "无待确认节点".into(); }
                    }
                    KeyCode::Char('x') if app.tab == Tab::Cluster => {
                        if let Some(d) = app.disc_state.selected().and_then(|i| app.discovered.get(i)).cloned() {
                            match discovered_action("reject", &d.node) {
                                Ok(_) => app.status = format!("已拒绝 {}", d.node),
                                Err(e) => app.status = format!("拒绝失败: {}", e),
                            }
                            app.refresh_cluster();
                        } else { app.status = "无待确认节点".into(); }
                    }
                    KeyCode::Up if app.tab == Tab::Services => app.prev(),
                    KeyCode::Char('e') if app.tab == Tab::Personas => {
                        if let Some(p) = app.persona_state.selected().and_then(|i| app.personas.get(i)) {
                            let pf = PathBuf::from(&p.home).join("PERSONA.md");
                            let ed = std::env::var("EDITOR").unwrap_or_else(|_| "nvim".into());
                            if let Some(t) = suspend_run(&ed, &[pf.to_str().unwrap_or("")], true) { terminal = t; }
                            app.reload_all();
                        }
                    }
                    KeyCode::Char('e') if app.tab == Tab::Settings => {
                        let cf = ensure_center_config();
                        let ed = std::env::var("EDITOR").unwrap_or_else(|_| "nvim".into());
                        if let Some(t) = suspend_run(&ed, &[cf.to_str().unwrap_or("")], true) { terminal = t; }
                        app.status = "中心地址已编辑".into();
                    }
                    KeyCode::Char('k') if app.tab == Tab::Services => {
                        if let Some(s) = app.service_state.selected().and_then(|i| app.services.get(i)) {
                            let uid = format!("gui/{}/{}", libc_getuid(), s.label);
                            let _ = Command::new("launchctl").arg("kickstart").arg("-k").arg(&uid).status();
                            app.status = format!("kickstart {}", s.label);
                            app.services = read_services();
                        }
                    }
                    KeyCode::Char('u') if app.tab == Tab::Services => {
                        let script = home().join("mahaul/agents/agent-update.sh");
                        let _ = Command::new("bash").arg(script).arg("--all").spawn();
                        app.status = "已后台跑 agent-update --all(升级+空闲重起)".into();
                    }
                    _ => {}
                }
            }
        }
    }
    ratatui::restore();
    Ok(())
}

// 取 uid(launchctl kickstart 用 gui/<uid>/<label>)
fn libc_getuid() -> u32 {
    std::env::var("UID").ok().and_then(|s| s.parse().ok()).unwrap_or_else(|| {
        Command::new("id").arg("-u").output().ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .and_then(|s| s.trim().parse().ok()).unwrap_or(501)
    })
}
