"""The owner page (/configure): one self-contained HTML document, no external resources.

Data from the server is only ever inserted with textContent, never as HTML.
"""

OWNER_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>OpenSeek Cloud</title>
<style>
:root{--bg:#0d0e12;--surface:#13151b;--card:#181b23;--hi:#1f2330;--border:#252a36;--text:#e9eaf0;--muted:#8b91a1;
--faint:#5b6171;--accent:#8b7bff;--accent2:#5ad1c1;--good:#4fd68a;--warn:#f0b44c;--bad:#ef6a6a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:720px;margin:0 auto;padding:32px 16px 64px}
header{display:flex;align-items:center;gap:12px;margin-bottom:28px}
.logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2))}
h1{font-size:20px;margin:0;font-weight:650;letter-spacing:.2px}
h1 small{display:block;color:var(--muted);font-size:13px;font-weight:400}
h2{font-size:15px;margin:0 0 12px;font-weight:600}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:16px}
.muted{color:var(--muted)}.faint{color:var(--faint);font-size:13px}
button{font:inherit;color:var(--text);background:var(--hi);border:1px solid var(--border);border-radius:9px;
padding:8px 14px;cursor:pointer}
button:hover{border-color:#3a4155}
button.primary{background:var(--accent);border:none;color:#0d0e12;font-weight:600}
button.danger{color:var(--bad);background:transparent}
button:disabled{opacity:.5;cursor:default}
input{font:inherit;color:var(--text);background:var(--surface);border:1px solid var(--border);border-radius:9px;
padding:9px 12px;width:100%}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{background:var(--surface);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:14px;font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px}
.ok{background:var(--good)}.warn{background:var(--warn)}.bad{background:var(--bad)}
.code{font:600 34px/1.1 ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:4px;margin:6px 0}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all;font-size:13px;
background:var(--surface);border-radius:8px;padding:8px 10px}
ul{list-style:none;padding:0;margin:0}
li{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--border)}
li:first-child{border-top:none}
.hidden{display:none}
.msg{color:var(--bad);min-height:1.5em;margin-top:8px}
@media (prefers-color-scheme: light){:root:not([data-theme=dark]){--bg:#f5f6fa;--surface:#eceef4;--card:#fff;
--hi:#f0f1f6;--border:#dfe2ea;--text:#171a22;--muted:#5d6475;--faint:#8a90a0}}
</style>
</head>
<body>
<main>
<header><div class="logo" aria-hidden="true"></div><h1>OpenSeek Cloud<small>Owner page</small></h1></header>

<section id="cfg" class="card hidden"><h2>Setup needed</h2>
<p class="muted">The server is missing a setting:</p><p class="mono" id="cfgmsg"></p>
<p class="faint">Add it in your hosting project's environment variables, then redeploy. See docs/CLOUD.md.</p></section>

<section id="login" class="card hidden"><h2>Sign in</h2>
<form id="loginf" class="row"><input id="pin" type="password" autocomplete="current-password"
placeholder="Owner PIN (OPENSEEK_ADMIN_PIN)" style="flex:1;min-width:200px"><button class="primary">Sign in</button></form>
<div class="msg" id="loginmsg"></div></section>

<div id="app" class="hidden">
<section class="card"><h2>Connect a TV</h2>
<p class="muted">Open the pairing window, then enter the address and code in the TV app.</p>
<div id="pairoff"><button class="primary" id="pairbtn">Show a pairing code</button></div>
<div id="pairon" class="hidden"><div class="faint">Server address</div><div class="mono" id="addr"></div>
<div class="faint" style="margin-top:12px">One-time code</div><div class="code" id="code"></div>
<div class="row"><span class="faint" id="left"></span><button id="pairclose">Close</button></div></div></section>

<section class="card"><h2>Devices</h2><ul id="devs"></ul><p class="faint hidden" id="nodevs">No devices yet.</p>
<details style="margin-top:12px"><summary class="muted">Add a device with a token instead</summary>
<form id="addf" class="row" style="margin-top:10px"><input id="dname" maxlength="64" placeholder="Device name"
style="flex:1;min-width:180px"><button>Create token</button></form>
<div id="tokout" class="hidden" style="margin-top:10px"><div class="faint">Token (shown once):</div>
<div class="mono" id="tok"></div></div></details></section>

<section class="card"><h2>Status</h2><div class="grid" id="stats"></div>
<div class="row" style="margin-top:12px"><button id="chk">Test storage</button><span class="faint" id="chkout"></span>
<span style="flex:1"></span><button class="danger" id="logout">Sign out</button></div></section>
</div>
</main>
<script>
"use strict";
const CONFIG_ERROR = /*CONFIG_ERROR*/null;
let csrf = "", timer = null;
const $ = id => document.getElementById(id);
const show = (id, on) => $(id).classList.toggle("hidden", !on);
async function api(path, body){
  const opt = {method: body === undefined ? "GET" : "POST", headers: {}, credentials: "same-origin"};
  if (body !== undefined){ opt.headers["Content-Type"] = "application/json"; opt.headers["X-OpenSeek-CSRF"] = csrf;
    opt.body = JSON.stringify(body); }
  const r = await fetch("/configure/api/" + path, opt);
  let j = {}; try { j = await r.json(); } catch (e) {}
  return {status: r.status, data: j};
}
function stat(label, value, cls){
  const d = document.createElement("div"); d.className = "stat";
  const b = document.createElement("b");
  if (cls){ const s = document.createElement("span"); s.className = "dot " + cls; b.appendChild(s); }
  b.appendChild(document.createTextNode(value));
  const l = document.createElement("span"); l.className = "faint"; l.textContent = label;
  d.append(b, l); return d;
}
async function load(){
  const r = await api("state");
  if (r.status === 401){ show("login", true); show("app", false); return; }
  if (r.status !== 200){ return; }
  const s = r.data; csrf = s.csrf;
  show("login", false); show("app", true);
  $("addr").textContent = s.server || "";
  const st = $("stats"); st.replaceChildren(
    stat("Key-value store", s.kv === "upstash" ? "Upstash" : "Memory only", s.kv_ok ? (s.kv_persistent ? "ok" : "warn") : "bad"),
    stat("Tile storage", {r2: "Cloudflare R2", cloudinary: "Cloudinary", local: "Local disk"}[s.storage] || s.storage, "ok"),
    stat("Pre-generation", s.pregen, null), stat("Extraction", s.engine, null), stat("Version", s.version, null));
  const ul = $("devs"); ul.replaceChildren();
  for (const d of s.devices){
    const li = document.createElement("li"); const left = document.createElement("div");
    const n = document.createElement("div"); n.textContent = d.name;
    const meta = document.createElement("div"); meta.className = "faint";
    meta.textContent = d.device_id + " · added " + new Date(d.created * 1000).toLocaleDateString();
    left.append(n, meta);
    const b = document.createElement("button"); b.className = "danger"; b.textContent = "Remove";
    b.onclick = async () => { if (confirm("Remove " + d.name + "?")){ await api("devices/" + d.device_id + "/revoke", {}); load(); } };
    li.append(left, b); ul.appendChild(li);
  }
  show("nodevs", s.devices.length === 0);
  if (!s.pairing_open && timer === null){ show("pairon", false); show("pairoff", true); }
}
$("loginf").onsubmit = async e => {
  e.preventDefault(); $("loginmsg").textContent = "";
  const r = await api("login", {pin: $("pin").value});
  if (r.status === 200){ csrf = r.data.csrf; $("pin").value = ""; load(); }
  else $("loginmsg").textContent = r.status === 429 ? "Too many tries. Wait a minute." : "Wrong PIN.";
};
$("pairbtn").onclick = async () => {
  const r = await api("pairing/open", {}); if (r.status !== 200) return;
  $("code").textContent = r.data.code; show("pairoff", false); show("pairon", true);
  const end = r.data.expires * 1000; clearInterval(timer);
  const tick = () => { const s = Math.max(0, Math.round((end - Date.now()) / 1000));
    $("left").textContent = "Valid for " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
    if (s === 0){ clearInterval(timer); timer = null; show("pairon", false); show("pairoff", true); } };
  tick(); timer = setInterval(tick, 1000);
};
$("pairclose").onclick = async () => { await api("pairing/close", {}); clearInterval(timer); timer = null;
  show("pairon", false); show("pairoff", true); load(); };
$("addf").onsubmit = async e => {
  e.preventDefault(); const name = $("dname").value.trim(); if (!name) return;
  const r = await api("devices", {name});
  if (r.status === 200){ $("tok").textContent = r.data.token; show("tokout", true); $("dname").value = ""; load(); }
};
$("chk").onclick = async () => { $("chkout").textContent = "Testing…"; const r = await api("storage/check", {});
  $("chkout").textContent = r.data.ok ? "Storage works." : "Storage failed. Check the keys."; };
$("logout").onclick = async () => { await api("logout", {}); csrf = ""; show("app", false); show("login", true); };
if (CONFIG_ERROR){ $("cfgmsg").textContent = CONFIG_ERROR; show("cfg", true); }
else { load(); setInterval(() => { if (!$("app").classList.contains("hidden")) load(); }, 15000); }
</script>
</body>
</html>
"""
