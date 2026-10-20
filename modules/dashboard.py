"""The dashboard (RESKATE_MODE=dashboard): a small password-protected web page to watch the servers, the
rollover, the bridge and the bots. Stage 1 starts and stops nothing; the only write is a chat line (see Chat).

* Servers, slots, players and map come from the bridge (GET /v1/servers), the live log from its events
  (GET /v1/events). Per server the page says whether a restart would be allowed (at most DASHBOARD_CUTOFF
  players, default 3) or only a rollover copy; an unknown player count counts as busy.
* Containers (rollover, bridge, bot ... and the game servers) and the last log lines of the tools come from
  the Docker socket when DASHBOARD_DOCKER points at it (mount it read-only; only GETs are made). Without it
  that part of the page just says so.
* Login: DASHBOARD_PASSWORD (required, the dashboard refuses to start without it). Successful logins get a
  signed, HttpOnly, SameSite=Strict session cookie (Secure behind HTTPS, i.e. when the reverse proxy sends
  X-Forwarded-Proto: https); sessions end after DASHBOARD_SESSION_HOURS (default 12) or a restart. Five wrong
  passwords from one address lock it out for five minutes.
* Chat: one tab per server with the chat from the bridge (scope chat). Typing in the box sends a `say` through the
  bridge's console action (the only thing the dashboard writes); DASHBOARD_SAY=false makes the chat read-only.
  A send needs the session cookie plus the X-Requested-With header the page sets, and is logged by the bridge
  as an audit event ("dashboard").
* Moderator: search players over all servers (bridge GET /v1/servers/<n>/players), then kick, ban, private message,
  teleport (only when DASHBOARD_TP_COMMAND is set), and per server say, announce, poll. Everything goes through
  the bridge's console actions, so the bridge logs each one as an audit event ("dashboard"); restarts and stops
  stay refused there. DASHBOARD_MOD=false turns the whole section (and the chat box) read-only.
* It listens on DASHBOARD_BIND:DASHBOARD_PORT (127.0.0.1:8790) in plain HTTP; put a reverse proxy with
  HTTPS in front of it for the subdomain.

Settings: DASHBOARD_PASSWORD, BRIDGE_URL, BRIDGE_TOKEN, DASHBOARD_BIND, DASHBOARD_PORT, DASHBOARD_CUTOFF,
DASHBOARD_DOCKER (socket path, default none), DASHBOARD_SESSION_HOURS, DASHBOARD_SAY (default true), DASHBOARD_MOD (default true), DASHBOARD_TP_COMMAND
(console line for a teleport, e.g. "tp {player} {target}"; unset = no teleport buttons). Standard library only."""
import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8780").rstrip("/")
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
BIND = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8790"))
CUTOFF = int(os.environ.get("DASHBOARD_CUTOFF", "3"))
DOCKER = os.environ.get("DASHBOARD_DOCKER", "").strip()
SAY = os.environ.get("DASHBOARD_SAY", "true").strip().lower() not in ("false", "0", "off", "no")
MOD = os.environ.get("DASHBOARD_MOD", "true").strip().lower() not in ("false", "0", "off", "no")
TP = os.environ.get("DASHBOARD_TP_COMMAND", "").strip()
SESSION = int(float(os.environ.get("DASHBOARD_SESSION_HOURS", "12")) * 3600)
KEY = secrets.token_bytes(32)
COOKIE = "reskate_dash"
TOOLS = ("rollover", "bridge", "bot", "bot-chat", "bot-admin", "leaderboard", "webhooks", "settings",
         "announcer", "leaderboard-backup", "dashboard")
fails = {}           # address -> [count, locked until]


def say(text):
    print(text, flush=True)


def restart_rule(players):
    """What the project rule allows for a server with this many players (None = unknown = busy)."""
    if players is not None and players <= CUTOFF:
        return "restart"
    return "rollover"


def sign(expires):
    return f"{expires}.{hmac.new(KEY, str(expires).encode(), hashlib.sha256).hexdigest()}"


def valid(token):
    try:
        expires, mac = token.split(".", 1)
        return hmac.compare_digest(sign(int(expires)), f"{expires}.{mac}") and int(expires) > time.time()
    except ValueError:
        return False


def bridge(path, body=None):
    """GET on the bridge, or a POST of `body` (a dict) when given."""
    headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BRIDGE_URL + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def line(value, limit):
    value = " ".join(str(value or "").split())[:limit]
    if not value:
        raise ValueError("a text is missing")
    return value


def moderate(body):
    """One moderator action through the bridge: {server, slot?, action, player?, steam_id?, text?, target?}."""
    server, action = str(body.get("server", "")), str(body.get("action", ""))
    if not server.isalnum():
        raise ValueError("a server is needed")
    slot = str(body.get("slot") or "")[:2] or None
    if action in ("say", "announce"):
        todo = {"action": action, "args": {"text": line(body.get("text"), 200)}}
    elif action == "msg":
        todo = {"action": "msg", "args": {"player": line(body.get("player"), 64), "text": line(body.get("text"), 190)}}
    elif action == "kick":
        todo = {"action": "kick", "args": {"player": line(body.get("player"), 64)}}
    elif action == "ban":
        steam_id = str(body.get("steam_id", ""))
        if not (steam_id.isdigit() and len(steam_id) == 17):
            raise ValueError("a ban needs the 17-digit Steam ID")
        reason = " ".join(str(body.get("text") or "").split())[:100]
        todo = {"action": "console", "args": {"command": f"ban {steam_id} {reason}".strip()}}
    elif action == "poll":
        todo = {"action": "poll", "args": {"question": line(body.get("text"), 120), "answers": line(body.get("answers"), 280)}}
    elif action == "tp":
        if not TP:
            raise ValueError("teleport is not set up (DASHBOARD_TP_COMMAND)")
        player, target = line(body.get("player"), 64), line(body.get("target"), 64)
        todo = {"action": "console", "args": {"command": TP.format(player=player, target=target)}}
    else:
        raise ValueError(f"unknown action {action!r}")
    if slot:
        todo["slot"] = slot
    todo["by"] = "dashboard"
    return bridge(f"/v1/servers/{server}/do", todo)


def players():
    """Everybody who is on, from every running server: [{name, steam_id, team, server, slot, role}]."""
    out = []
    for r in bridge("/v1/servers"):
        if not r.get("running"):
            continue
        try:
            for p in bridge(f"/v1/servers/{r['server']}/players"):
                out.append(dict(p, server=r["server"], role=r.get("role", "")))
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return out


class Unix(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("docker", timeout=10)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(self.path)


def docker_get(path):
    """GET on the Docker socket (read-only: the dashboard never sends anything else)."""
    conn = Unix(DOCKER)
    try:
        conn.request("GET", "/v1.41" + path)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"docker {resp.status}")
        return data
    finally:
        conn.close()


def demux(data):
    """Text of a Docker log stream (8-byte frame headers unless the container has a TTY)."""
    out, i = [], 0
    while i + 8 <= len(data) and data[i] in (0, 1, 2) and data[i + 1:i + 4] == b"\0\0\0":
        size = int.from_bytes(data[i + 4:i + 8], "big")
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode("utf-8", "replace") if out else data.decode("utf-8", "replace")


def containers():
    if not DOCKER:
        return {"docker": False, "containers": []}
    rows = []
    for c in json.loads(docker_get("/containers/json?all=1")):
        name = c["Names"][0].lstrip("/")
        if not name.startswith("reskate"):
            continue
        rows.append({"name": name, "image": c["Image"], "state": c["State"], "status": c["Status"],
                     "tool": name.removeprefix("reskate-") in TOOLS})
    return {"docker": True, "containers": sorted(rows, key=lambda r: (not r["tool"], r["name"]))}


def tool_log(name, lines=60):
    if not DOCKER or not name.removeprefix("reskate-") in TOOLS:
        raise ValueError("unknown tool")
    data = docker_get(f"/containers/{name}/logs?stdout=1&stderr=1&tail={max(1, min(lines, 300))}")
    return {"name": name, "log": demux(data)}


def servers():
    rows = bridge("/v1/servers")
    for r in rows:
        r["rule"] = restart_rule(r.get("players")) if r.get("running") else "empty"
        for s in r.get("slots", {}).values():
            s["rule"] = restart_rule(s.get("players")) if s.get("running") else "empty"
    return {"cutoff": CUTOFF, "say": SAY and MOD, "mod": MOD, "tp": bool(TP), "servers": rows}


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReSkate Dashboard</title><style>
:root{--bg:#f5f6f8;--fg:#1c2230;--card:#fff;--line:#d9dde5;--mut:#667085;--ok:#1a7f4b;--warn:#b25e09;--bad:#b42318;--acc:#3b5bdb}
@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e6e9ef;--card:#1b2029;--line:#2c3340;--mut:#9aa3b2;--ok:#4cc38a;--warn:#f0a53c;--bad:#f97066;--acc:#8ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:8px 12px;flex-wrap:wrap;align-items:center;padding:12px max(16px,env(safe-area-inset-right,0px)) 12px max(16px,env(safe-area-inset-left,0px));border-bottom:1px solid var(--line)}header h1{font-size:17px;margin:0;flex:1 1 auto}
main{padding:20px max(16px,env(safe-area-inset-right,0px)) 32px max(16px,env(safe-area-inset-left,0px));max-width:1100px;margin:auto;display:grid;grid-template-columns:minmax(0,1fr);gap:28px}section h2{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.card h3{margin:0;font-size:15px;min-width:0;overflow-wrap:anywhere}.head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:6px}.pill{white-space:nowrap}
.mut{color:var(--mut);font-size:13px}.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
#chat{min-height:120px}pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;margin:0;max-height:320px;overflow:auto;font:12px/1.4 ui-monospace,monospace;white-space:pre-wrap;word-break:break-all}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:middle}th{color:var(--mut);font-weight:500}
td .act{display:flex;gap:6px;flex-wrap:wrap}td button{padding:3px 8px;font-size:13px}button.arm{border-color:var(--bad);color:var(--bad)}
#find,#srvtools{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 8px}#srvtools{margin:8px 0 0}#find input,#srvtools input{flex:1;min-width:140px;font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}.tabs button.on{border-color:var(--acc);color:var(--acc)}
#sayform{display:flex;gap:8px;margin-top:8px}#sayform input{flex:1;min-width:0;font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
button,select{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 10px}button{cursor:pointer}
</style></head><body>
<header><h1>ReSkate Dashboard</h1><span class="mut" id="upd"></span><button onclick="fetch('/logout',{method:'POST'}).then(()=>location.reload())">Abmelden</button></header>
<main>
<section><h2>Server</h2><div class="grid" id="servers"></div></section>
<section><h2>Tools &amp; Container</h2><div class="grid" id="containers"></div></section>
<section><h2>Chat</h2><div class="tabs" id="tabs"></div><pre id="chat">-</pre>
<form id="sayform" autocomplete="off"><input id="saytext" maxlength="200" placeholder="Nachricht an den Server-Chat"><button>Senden</button></form></section>
<section id="modsec"><h2>Moderator</h2>
<form id="find" autocomplete="off"><input id="q" placeholder="Spieler suchen (Name, Steam-ID, Team)"><input id="mtext" maxlength="200" placeholder="Text / Grund (Nachricht, Ban) oder Ziel-Spieler (Teleport)"></form>
<div class="tablewrap"><table id="plist"><thead><tr><th>Spieler</th><th>Server</th><th>Aktionen</th></tr></thead><tbody></tbody></table></div>
<form id="srvtools" autocomplete="off"><select id="msrv"></select><input id="stext" maxlength="200" placeholder="Text an alle auf diesem Server">
<button type="button" data-a="say">Say</button><button type="button" data-a="announce">Announce</button></form>
<div class="mut" id="mres"></div></section>
<section><h2>Live-Log <select id="sel"><option value="">alle Server</option></select> <select id="scope"><option>log</option><option>public</option><option>admin</option><option>chat</option><option>audit</option></select></h2><pre id="events"></pre></section>
<section><h2>Tool-Log <select id="tool"></select></h2><pre id="toollog">-</pre></section>
</main>
<script>
const $=id=>document.getElementById(id);
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
const RULE={restart:["Neustart erlaubt","ok"],rollover:["nur Rollover","warn"],empty:["aus","mut"]};
async function api(p){const r=await fetch(p);if(r.status==401){location.reload();throw 0}if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json()}
async function servers(){
 const d=await api('/api/servers'),box=$('servers');box.replaceChildren();
 const sel=$('sel');if(sel.options.length==1)d.servers.forEach(s=>sel.add(new Option(s.server+' '+s.role,s.server)));
 tabs(d);mod(d);
 for(const s of d.servers){
  const c=el('div','card'),[t,k]=RULE[s.rule]||[s.rule,'mut'],h=el('div','head');
  h.append(el('h3',0,'Server '+s.server+(s.role?' · '+s.role:'')),Object.assign(el('span','pill '+k,t),{title:'Grenze: '+d.cutoff+' Spieler'}));c.append(h);
  c.append(el('div',0,s.running?(s.players==null?'Spieler unbekannt':s.players+'/'+(s.max||'?')+' Spieler')+' · '+(s.map||'?'):'offline'));
  for(const [l,x] of Object.entries(s.slots||{})){
   const [st,sk]=RULE[x.rule]||['','mut'];
   c.append(el('div','mut','Slot '+l+': '+(x.running?(x.version||'?')+' · '+(x.players==null?'?':x.players)+' Spieler':'aus')));
  }
  box.append(c)}
}
async function containers(){
 const d=await api('/api/containers'),box=$('containers');box.replaceChildren();
 if(!d.docker){box.append(el('div','mut','Docker-Socket nicht eingebunden (DASHBOARD_DOCKER).'));return}
 const t=$('tool');if(!t.options.length)d.containers.filter(c=>c.tool).forEach(c=>t.add(new Option(c.name)));
 for(const c of d.containers){const e=el('div','card'),run=c.state=='running';
  const hd=el('div','head');hd.append(el('h3',0,c.name),el('span','pill '+(run?'ok':'bad'),c.state));
  e.append(hd,el('div','mut',c.image+' · '+c.status));box.append(e)}
}
let people=[],modOn=true,tpOn=false;
function mod(d){modOn=d.mod;tpOn=d.tp;$('modsec').hidden=!d.mod;const m=$('msrv');
 if(!m.options.length)d.servers.forEach(s=>m.add(new Option(s.server+' '+s.role,s.server)))}
async function who(){if(!modOn)return;people=await api('/api/players');draw()}
function say_(m){$('mres').textContent=m}
async function act(body,b){
 if(b&&!b.classList.contains('arm')&&/^(kick|ban)$/.test(body.action)){b.classList.add('arm');const t=b.textContent;b.textContent='Sicher?';setTimeout(()=>{b.classList.remove('arm');b.textContent=t},3000);return}
 const r=await fetch('/api/mod',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify(body)});
 const d=await r.json();say_(r.ok?'Gesendet: '+body.action+(body.player?' '+body.player:''):'Fehler: '+(d.error||r.status));if(r.ok)setTimeout(who,1500)}
function draw(){const q=$('q').value.trim().toLowerCase(),tb=document.querySelector('#plist tbody');tb.replaceChildren();
 const rows=people.filter(p=>!q||(p.name+' '+p.steam_id+' '+(p.team||'')).toLowerCase().includes(q));
 if(!rows.length){const tr=el('tr'),td=el('td','mut',people.length?'Kein Treffer.':'Niemand online.');td.colSpan=3;tr.append(td);tb.append(tr);return}
 for(const p of rows){const tr=el('tr'),n=el('td'),s=el('td','mut'),a=el('td'),w=el('div','act');
  n.append(el('div',0,(p.team?'['+p.team+'] ':'')+p.name),el('div','mut',p.steam_id));s.textContent=p.server+(p.slot||'')+' '+p.role;
  const base={server:p.server,slot:p.slot,player:p.name,steam_id:p.steam_id};
  const add=(l,f)=>{const b=el('button',0,l);b.onclick=()=>f(b);w.append(b)};
  add('Nachricht',()=>act({...base,action:'msg',text:$('mtext').value}));
  add('Kick',b=>act({...base,action:'kick'},b));
  add('Ban',b=>act({...base,action:'ban',text:$('mtext').value},b));
  if(tpOn)add('Teleport',()=>act({...base,action:'tp',target:$('mtext').value}));
  a.append(w);tr.append(n,s,a);tb.append(tr)}}
$('q').oninput=draw;$('find').onsubmit=e=>e.preventDefault();
for(const b of document.querySelectorAll('#srvtools button'))b.onclick=()=>act({server:$('msrv').value,action:b.dataset.a,text:$('stext').value}).then(()=>{$('stext').value=''});
let chatSrv='*',chatLast=0,chatLines=[],canSay=true;
function tabs(d){canSay=d.say;$('sayform').hidden=!d.say;const t=$('tabs');if(t.children.length)return;
 const all=el('button',0,'Alle Chats');all.dataset.s='*';all.onclick=()=>pick('*');t.append(all);
 for(const s of d.servers){const b=el('button',0,s.server+(s.role?' '+s.role:''));b.dataset.s=s.server;
  b.onclick=()=>pick(s.server);t.append(b)}mark()}
function pick(v){chatSrv=v;chatLast=0;chatLines=[];$('chat').textContent='-';mark();chat()}
function mark(){for(const b of $('tabs').children)b.classList.toggle('on',b.dataset.s==chatSrv);
 const i=$('saytext');i.disabled=chatSrv=='*';i.placeholder=chatSrv=='*'?'Server-Tab wählen, um zu schreiben':'Nachricht an den Server-Chat'}
async function chat(){if(!chatSrv)return;const srv=chatSrv;
 const d=await api('/api/events?'+new URLSearchParams({after:chatLast,server:srv=='*'?'':srv,scope:'chat'}));
 if(srv!=chatSrv)return;chatLast=d.last;
 for(const e of d.events)chatLines.push((e.time||'').slice(11,19)+(srv=='*'?' ['+e.server+']':'')+' '+e.text.replace(/^(\\[[^\\]]*\\]\\s*)?\\[chat\\]\\s*/,''));
 chatLines.splice(0,Math.max(0,chatLines.length-300));const p=$('chat'),b=p.scrollTop+p.clientHeight>=p.scrollHeight-20;
 if(d.events.length||p.textContent=='-'){p.textContent=chatLines.join('\\n')||'(noch keine Chat-Nachrichten)';if(b)p.scrollTop=p.scrollHeight}}
$('sayform').onsubmit=async ev=>{ev.preventDefault();const t=$('saytext').value.trim();if(!t||chatSrv=='*')return;
 const r=await fetch('/api/say',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'dashboard'},body:JSON.stringify({server:chatSrv,text:t})});
 if(r.ok){$('saytext').value='';chat()}else $('saytext').placeholder=(await r.json()).error||'Fehler'};
let last=0;const lines=[];
async function events(){
 const q=new URLSearchParams({after:last,server:$('sel').value,scope:$('scope').value});
 const d=await api('/api/events?'+q);last=d.last;
 for(const e of d.events)lines.push((e.time||'').slice(11,19)+' ['+e.server+(e.slot?e.slot:'')+'] '+e.text);
 lines.splice(0,Math.max(0,lines.length-400));const p=$('events'),b=p.scrollTop+p.clientHeight>=p.scrollHeight-20;
 if(d.events.length){p.textContent=lines.join('\\n');if(b)p.scrollTop=p.scrollHeight}
}
for(const id of['sel','scope'])$(id).onchange=()=>{last=0;lines.length=0;$('events').textContent=''};
async function toollog(){const n=$('tool').value;if(n)$('toollog').textContent=(await api('/api/toollog?name='+n)).log}
$('tool').onchange=toollog;
async function tick(){
 for(const f of[servers,who,chat,containers,events,toollog])try{await f()}catch(e){if(e)console.warn(e)}
 $('upd').textContent='aktualisiert '+new Date().toLocaleTimeString()}
tick();setInterval(tick,5000);
</script></body></html>"""

LOGIN = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ReSkate Dashboard</title><style>
:root{--bg:#f5f6f8;--fg:#1c2230;--line:#d9dde5}@media(prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e6e9ef;--line:#2c3340}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}form{max-width:320px;margin:20vh auto;padding:0 16px;display:grid;gap:10px}
input,button{font:inherit;padding:9px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}p{color:#b42318;margin:0}
</style></head><body><form method="post" action="/login"><h2>ReSkate Dashboard</h2>%s
<input type="password" name="password" placeholder="Passwort" autofocus required><button>Anmelden</button></form></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def address(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[-1].strip()

    def send(self, code, body, kind="text/html; charset=utf-8", headers=()):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, code, body):
        self.send(code, json.dumps(body, ensure_ascii=False), "application/json; charset=utf-8")

    def logged_in(self):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return COOKIE in jar and valid(jar[COOKIE].value)

    def cookie(self, value, age):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return ("Set-Cookie", f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}{secure}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self.json(200, {"ok": True})
        if not self.logged_in():
            if url.path.startswith("/api/"):
                return self.json(401, {"error": "login"})
            return self.send(200, LOGIN % "")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                return self.send(200, PAGE)
            if url.path == "/api/servers":
                return self.json(200, servers())
            if url.path == "/api/players":
                return self.json(200, players() if MOD else [])
            if url.path == "/api/containers":
                return self.json(200, containers())
            if url.path == "/api/toollog":
                return self.json(200, tool_log(q.get("name", "")))
            if url.path == "/api/events":
                path = "/v1/events?" + "&".join(f"{k}={q[k]}" for k in ("after", "server", "scope") if q.get(k))
                return self.json(200, bridge(path))
        except ValueError as e:
            return self.json(400, {"error": str(e)})
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            return self.json(502, {"error": f"bridge or docker not reachable: {e}"})
        self.json(404, {"error": "not found"})

    def say(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not (SAY and MOD) or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "sending is off" if not (SAY and MOD) else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            moderate({"server": body.get("server", ""), "action": "say", "text": body.get("text", "")})
        except (ValueError, TypeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self.json(502, {"error": f"bridge: {e.code} {e.read()[:200].decode(errors='replace')}"})
        except (urllib.error.URLError, OSError) as e:
            return self.json(502, {"error": f"bridge not reachable: {e}"})
        say(f"dashboard: chat sent to server {body.get('server')}")
        self.json(200, {"ok": True})

    def mod(self):
        if not self.logged_in():
            return self.json(401, {"error": "login"})
        if not MOD or self.headers.get("X-Requested-With") != "dashboard":
            return self.json(403, {"error": "moderation is off" if not MOD else "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            moderate(body)
        except (ValueError, TypeError, AttributeError) as e:
            return self.json(400, {"error": str(e)})
        except urllib.error.HTTPError as e:
            return self.json(502, {"error": f"bridge: {e.code} {e.read()[:200].decode(errors='replace')}"})
        except (urllib.error.URLError, OSError) as e:
            return self.json(502, {"error": f"bridge not reachable: {e}"})
        say(f"dashboard: {body.get('action')} on server {body.get('server')}")
        self.json(200, {"ok": True})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/logout":
            return self.send(200, "ok", headers=[self.cookie("", 0)])
        if path == "/api/say":
            return self.say()
        if path == "/api/mod":
            return self.mod()
        if path != "/login":
            return self.json(404, {"error": "not found"})
        who = self.address()
        count, until = fails.get(who, [0, 0])
        if until > time.time():
            return self.send(429, LOGIN % "<p>Zu viele Versuche, bitte später erneut.</p>")
        form = parse_qs(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)).decode(errors="replace"))
        given = form.get("password", [""])[0]
        if hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest()):
            fails.pop(who, None)
            return self.send(303, "", headers=[("Location", "/"), self.cookie(sign(int(time.time()) + SESSION), SESSION)])
        count += 1
        fails[who] = [count, time.time() + 300 if count >= 5 else 0]
        time.sleep(1)
        say(f"dashboard: wrong password from {who}")
        self.send(401, LOGIN % "<p>Falsches Passwort.</p>")


def main():
    if len(PASSWORD) < 8:
        sys.exit("dashboard: DASHBOARD_PASSWORD (at least 8 characters) is not set; refusing to start without a login")
    if not BRIDGE_TOKEN:
        sys.exit("dashboard: BRIDGE_TOKEN is not set")
    say(f"dashboard: listening on {BIND}:{PORT} (restart rule: at most {CUTOFF} players)")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
