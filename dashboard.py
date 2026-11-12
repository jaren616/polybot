"""Local browser dashboard at http://localhost:8765 (only reachable from this computer)."""
import json
import statistics
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
from db import day_start


def state(db) -> dict:
    m = db.money()
    closed = db.q("SELECT * FROM positions WHERE status='closed' ORDER BY closed_ts DESC")
    wins = sum(1 for p in closed if p["pnl"] > 0)
    graded = [p for p in closed if p["est_prob"] is not None and p["yes_final"] is not None]
    brier = brier_mkt = None
    if graded:
        brier = statistics.mean((p["est_prob"] - p["yes_final"]) ** 2 for p in graded)
        brier_mkt = statistics.mean((p["market_yes"] - p["yes_final"]) ** 2 for p in graded)
    eq = db.q("SELECT ts, equity FROM equity ORDER BY ts")
    if len(eq) > 600:  # thin out long histories
        step = len(eq) // 600 + 1
        eq = eq[::step] + [eq[-1]]
    arbs = db.q("SELECT * FROM arbs ORDER BY first_seen DESC LIMIT 100")
    closed_arbs = [a for a in db.q("SELECT first_seen, last_seen FROM arbs WHERE open=0")]
    arb_med = statistics.median((a["last_seen"] - a["first_seen"]) / 60 for a in closed_arbs) if closed_arbs else None
    sig_counts = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM signals GROUP BY kind")}
    return {
        "now": time.time(),
        "mode": db.get("mode", config.MODE),
        "paused": db.paused(),
        "geo": db.get("geo"),
        "last": {k: db.get(k) for k in ("last_news", "last_slow", "last_settle", "started")},
        "markets_loaded": db.get("markets_loaded"),
        "slow_stats": db.get("slow_stats"),
        "limits": {k: getattr(config, k) for k in (
            "BANKROLL", "MAX_STAKE", "MAX_EXPOSURE", "MAX_OPEN_POSITIONS", "MAX_TRADES_PER_DAY",
            "DAILY_LOSS_LIMIT", "KELLY_FRACTION", "MIN_EDGE", "MIN_EDGE_HIGH", "HIGH_PRICE_ZONE", "MAX_PRICE")},
        "ai_on": bool(config.ANTHROPIC_API_KEY),
        "money": m,
        "today_pnl": m["equity"] - db.equity_at(day_start()),
        "trades_today": db.trades_today(),
        "stats": {"closed": len(closed), "wins": wins, "brier": brier, "brier_mkt": brier_mkt,
                  "arbs_total": len(db.q("SELECT key FROM arbs")), "arb_median_min": arb_med,
                  "signals": sig_counts},
        "equity": [[r["ts"], r["equity"]] for r in eq] + [[time.time(), m["equity"]]],
        "by_strategy": db.strategy_stats(),
        "odds": {k: v for k, v in (db.get("odds_cache") or {}).items() if k != "games"},
        "odds_on": bool(config.ODDS_API_KEY),
        "open": [dict(p) for p in db.open_positions()],
        "closed": [dict(p) for p in closed[:50]],
        "signals": [dict(s) | {"data": json.loads(s["data"] or "{}")}
                    for s in db.q("SELECT * FROM signals ORDER BY ts DESC LIMIT 150")],
        "arbs": [dict(a) for a in arbs],
        "logs": [dict(l) for l in db.q("SELECT * FROM logs ORDER BY id DESC LIMIT 60")],
    }


def export_static(db, path="site/index.html"):
    """Write a self-contained copy of the dashboard (for GitHub Pages: no server, no Pause button)."""
    import os
    st = state(db)
    st["static"] = True
    blob = json.dumps(st, default=str).replace("</", "<\\/")
    page = PAGE.replace("<title>Polybot Dashboard</title>",
                        '<title>Polybot Dashboard</title><meta http-equiv="refresh" content="120">')
    page = page.replace("<script>", f"<script>window.STATIC_STATE={blob};</script><script>", 1)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def make_handler(db):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json", code=200):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(PAGE, "text/html")
            elif self.path == "/api/state":
                self._send(json.dumps(state(db), default=str))
            else:
                self._send("{}", code=404)

        def do_POST(self):
            if self.path == "/api/pause":
                db.set("paused", True)
                db.log("Paused from dashboard")
            elif self.path == "/api/resume":
                db.set("paused", False)
                db.log("Resumed from dashboard")
            else:
                return self._send("{}", code=404)
            self._send(json.dumps({"paused": db.paused()}))
    return H


def serve(db, port=None):
    srv = ThreadingHTTPServer(("127.0.0.1", port or config.DASHBOARD_PORT), make_handler(db))
    return srv


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot Dashboard</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#14161b;--mute:#667085;--line:#e4e7ec;--soft:#f2f4f7;
--good:#067647;--goodbg:#ecfdf3;--bad:#c01048;--badbg:#fff1f3;--warn:#b54708;--warnbg:#fffaeb;
--acc:#1d4ed8;--accbg:#eff4ff;--news:#6941c6;--newsbg:#f4f3ff;--arb:#0e7090;--arbbg:#ecfdff}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f13;--card:#161920;--ink:#e9ebf0;--mute:#98a2b3;
--line:#252a33;--soft:#1d2129;--good:#47cd89;--goodbg:#0c2a1d;--bad:#fd6f8e;--badbg:#2e0f18;
--warn:#fdb022;--warnbg:#2a1d07;--acc:#84adff;--accbg:#13203f;--news:#bdb4fe;--newsbg:#211a3d;
--arb:#67e3f9;--arbbg:#0a2830}}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.pill{font-size:12px;font-weight:650;padding:3px 10px;border-radius:999px;background:var(--soft);color:var(--mute)}
.pill.paper{background:var(--accbg);color:var(--acc)}.pill.live{background:var(--badbg);color:var(--bad)}
.pill.run{background:var(--goodbg);color:var(--good)}.pill.pause{background:var(--warnbg);color:var(--warn)}
.sp{flex:1}.muted{color:var(--mute)}.small{font-size:12px}
button{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--ink);
padding:7px 14px;border-radius:9px;cursor:pointer}button:hover{background:var(--soft)}
button.danger{color:var(--bad)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kpi span{color:var(--mute);font-size:12px}.pos{color:var(--good)}.neg{color:var(--bad)}
.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:14px}
@media (max-width:900px){.grid{grid-template-columns:minmax(0,1fr)}}
svg{max-width:100%;display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px;min-width:0}
.card h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;gap:8px}
.card h2 .muted{font-weight:400}
.tbl{overflow-x:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:12px;color:var(--mute);font-weight:500;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td.r,th.r{text-align:right}tr:last-child td{border-bottom:0}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.q{max-width:380px}
.tag{font-size:11px;font-weight:650;padding:1px 7px;border-radius:6px;white-space:nowrap}
.t-news{background:var(--newsbg);color:var(--news)}.t-arb{background:var(--arbbg);color:var(--arb)}
.t-mover{background:var(--warnbg);color:var(--warn)}
.t-sports{background:var(--goodbg);color:var(--good)}.t-momentum{background:var(--accbg);color:var(--acc)}
.t-favourite,.t-favourites{background:var(--soft);color:var(--ink)}.t-YES{background:var(--goodbg);color:var(--good)}
.t-NO{background:var(--badbg);color:var(--bad)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.chip{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:3px 11px;font-size:12px;cursor:pointer;color:var(--mute)}
.chip.on{background:var(--ink);color:var(--card);border-color:var(--ink)}
.sig{padding:10px 0;border-bottom:1px solid var(--line)}.sig:last-child{border:0}
.sig .top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.sig .t{font-weight:600}.sig .d{color:var(--mute);margin:3px 0}.sig .a{font-size:12px}
.act-buy{color:var(--good);font-weight:600}
.empty{color:var(--mute);padding:16px 0;text-align:center}
svg text{fill:var(--mute);font-size:11px}
.log{font:12px/1.6 ui-monospace,Consolas,monospace;max-height:260px;overflow:auto}
.log .error{color:var(--bad)}.log .warn{color:var(--warn)}
.banner{border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:13px}
.banner.warn{background:var(--warnbg);color:var(--warn)}.banner.bad{background:var(--badbg);color:var(--bad)}
</style></head><body><div class="wrap">
<header><h1>Polybot</h1><span id="mode" class="pill">…</span><span id="run" class="pill">…</span>
<span class="sp"></span><span id="upd" class="muted small"></span><button id="toggle">Pause</button></header>
<div id="banners"></div>
<div class="kpis" id="kpis"></div>
<div class="card"><h2>By strategy</h2><div class="tbl" id="strat"></div></div>
<div class="grid"><div>
 <div class="card"><h2>Equity <span class="muted" id="eqsub"></span></h2><div id="chart"></div></div>
 <div class="card"><h2>Open positions <span class="muted" id="opensub"></span></h2><div class="tbl" id="open"></div></div>
 <div class="card"><h2>Closed trades</h2><div class="tbl" id="closed"></div></div>
 <div class="card"><h2>Arbitrage gaps <span class="muted" id="arbsub"></span></h2><div class="tbl" id="arbs"></div></div>
</div><div>
 <div class="card"><h2>Signals</h2><div class="chips" id="chips"></div><div id="sigs"></div></div>
 <div class="card"><h2>Risk limits</h2><div id="limits"></div></div>
 <div class="card"><h2>Activity log</h2><div class="log" id="log"></div></div>
</div></div></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd=(v,s)=>(s&&v>0?"+":"")+(v<0?"-$":"$")+Math.abs(v).toFixed(2);
const cls=v=>v>0?"pos":v<0?"neg":"";
const ago=t=>{if(!t)return"never";const s=Date.now()/1e3-t;return s<60?Math.round(s)+"s ago":s<3600?Math.round(s/60)+"m ago":Math.round(s/3600)+"h ago"};
const tm=t=>new Date(t*1e3).toLocaleString([], {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
let S=null, filter="all";
async function load(){if(window.STATIC_STATE){S=window.STATIC_STATE;render();return}try{const r=await fetch("/api/state");S=await r.json();render()}catch(e){$("upd").textContent="dashboard can't reach the bot - is it still running?"}}
async function post(p){await fetch(p,{method:"POST"});load()}
$("toggle").onclick=()=>post(S&&S.paused?"/api/resume":"/api/pause");
function render(){
 const m=S.money, st=S.stats;
 $("mode").textContent=S.mode.toUpperCase();$("mode").className="pill "+S.mode;
 $("run").textContent=S.paused?"PAUSED":"RUNNING";$("run").className="pill "+(S.paused?"pause":"run");
 $("toggle").textContent=S.paused?"Resume":"Pause";$("toggle").className=S.paused?"":"danger";
 $("upd").textContent=S.static?`cloud · last run ${ago(S.now)} · runs every ~10 min`:`news ${ago(S.last.last_news)} · scan ${ago(S.last.last_slow)}`;
 $("toggle").style.display=S.static?"none":"";
 let b="";
 if(!S.ai_on)b+=`<div class="banner warn">No ANTHROPIC_API_KEY set: news alerts appear, but the bot can't judge them, so it won't trade them.</div>`;
 if(S.paused)b+=`<div class="banner warn">Paused: no new trades. Scanning and alerts continue. ${S.static?"To resume, set the POLYBOT_PAUSED variable on GitHub to false.":"Press Resume to restart trading."}</div>`;
 if(S.mode==="live")b+=`<div class="banner bad">LIVE MODE: trades use real money${S.geo?` · connected from ${esc(S.geo.country||"?")}`:""}.</div>`;
 $("banners").innerHTML=b;
 const wr=st.closed?Math.round(100*st.wins/st.closed)+"%":"–";
 const br=st.brier==null?"–":`${st.brier.toFixed(3)} <span class="small ${st.brier<st.brier_mkt?"pos":"neg"}">vs ${st.brier_mkt.toFixed(3)}</span>`;
 $("kpis").innerHTML=[
  ["Equity",`$${m.equity.toFixed(2)}`,""],
  ["Today",usd(S.today_pnl,1),cls(S.today_pnl)],
  ["Total P&L",usd(m.equity-S.limits.BANKROLL,1),cls(m.equity-S.limits.BANKROLL)],
  ["Open / exposure",`${S.open.length} · $${m.open_cost.toFixed(0)}`,""],
  ["Win rate",`${wr} <span class="small muted">${st.closed} closed</span>`,""],
  ["Brier (you vs mkt)",br,""],
 ].map(([k,v,c])=>`<div class="kpi"><span>${k}</span><b class="${c}">${v}</b></div>`).join("");
 const SN={news:"AI news",sports:"Sports value",momentum:"Momentum",favourites:"Favourites"};
 const bs=S.by_strategy||[];
 $("strat").innerHTML=bs.length?`<table><tr><th>Strategy</th><th class="r">Open</th><th class="r">Closed</th><th class="r">Win rate</th><th class="r">Staked</th><th class="r">Realised</th><th class="r">Unrealised</th><th class="r">Total</th></tr>`+
  bs.map(r=>{const t=r.realized+r.unrealized;return`<tr><td><span class="tag t-${r.strategy}">${esc(SN[r.strategy]||r.strategy)}</span></td><td class="r">${r.open}</td><td class="r">${r.closed}</td><td class="r">${r.closed?Math.round(100*r.wins/r.closed)+"%":"–"}</td><td class="r">$${r.staked.toFixed(0)}</td><td class="r ${cls(r.realized)}">${usd(r.realized,1)}</td><td class="r ${cls(r.unrealized)}">${usd(r.unrealized,1)}</td><td class="r ${cls(t)}"><b>${usd(t,1)}</b></td></tr>`}).join("")+"</table>"
  +(S.odds_on?`<div class="small muted" style="margin-top:8px">Bookmaker odds ${S.odds.ts?"updated "+ago(S.odds.ts):"not fetched yet"}${S.odds.remaining!=null?` · ${S.odds.remaining} API credits left this month`:""}</div>`:`<div class="small muted" style="margin-top:8px">Sports value is off: set ODDS_API_KEY to turn it on.</div>`)
  :`<div class="empty">No trades yet.</div>`+(S.odds_on?"":`<div class="small muted">Sports value is off: set ODDS_API_KEY to turn it on.</div>`);
 chart();
 $("eqsub").textContent=`starting bankroll $${S.limits.BANKROLL}`;
 $("opensub").textContent=S.open.length?`${S.open.length} open`:"";
 $("open").innerHTML=S.open.length?`<table><tr><th>Market</th><th>Bought</th><th class="r">Entry</th><th class="r">Now</th><th class="r">Cost</th><th class="r">P&L</th><th>Opened</th></tr>`+
  S.open.map(p=>{const v=p.shares*(p.mark??p.price),u=v-p.cost;return`<tr><td class="q"><a href="${esc(p.url)}" target="_blank">${esc(p.question)}</a> <span class="small muted">${p.mode}</span></td><td><span class="tag t-${p.side}">${esc((p.label||p.side).slice(0,18))}</span> <span class="tag t-${p.strategy}">${esc(SN[p.strategy]||p.strategy)}</span></td><td class="r">${p.price.toFixed(3)}</td><td class="r">${(p.mark??p.price).toFixed(3)}</td><td class="r">$${p.cost.toFixed(2)}</td><td class="r ${cls(u)}">${usd(u,1)}</td><td class="small muted">${tm(p.ts)}</td></tr>`}).join("")+"</table>"
  :`<div class="empty">No open positions.</div>`;
 $("closed").innerHTML=S.closed.length?`<table><tr><th>Market</th><th>Bought</th><th class="r">Entry</th><th class="r">Your p</th><th>Result</th><th class="r">P&L</th></tr>`+
  S.closed.map(p=>`<tr><td class="q"><a href="${esc(p.url)}" target="_blank">${esc(p.question)}</a></td><td><span class="tag t-${p.side}">${esc((p.label||p.side).slice(0,18))}</span> <span class="tag t-${p.strategy}">${esc(SN[p.strategy]||p.strategy)}</span></td><td class="r">${p.price.toFixed(3)}</td><td class="r">${p.est_prob==null?"–":p.est_prob.toFixed(2)}</td><td>${esc(p.exit_reason||(p.pnl>0?"won":"lost"))}</td><td class="r ${cls(p.pnl)}">${usd(p.pnl,1)}</td></tr>`).join("")+"</table>"
  :`<div class="empty">Nothing settled yet. Markets can take weeks to resolve.</div>`;
 const ss=S.slow_stats;
 $("arbsub").textContent=ss?`${ss.events.toLocaleString()} events · ${ss.niche.toLocaleString()} niche · ${st.arbs_total} gaps logged`+(st.arb_median_min!=null?` · median life ${Math.round(st.arb_median_min)} min`:""):"first scan running…";
 $("arbs").innerHTML=S.arbs.length?`<table><tr><th>Seen</th><th>Type</th><th>Market</th><th class="r">Gap</th><th class="r">Max $</th><th class="r">Per year</th><th>Status</th></tr>`+
  S.arbs.slice(0,40).map(a=>`<tr><td class="small muted">${tm(a.first_seen)}</td><td><span class="tag t-arb">${esc(a.type)}</span></td><td class="q"><a href="${esc(a.url)}" target="_blank">${esc(a.event)}</a></td><td class="r">${(a.best_profit*100).toFixed(1)}c</td><td class="r">$${(a.best_profit*(a.last_size||0)).toFixed(2)}</td><td class="r ${a.apr<.05?"neg":"pos"}">${Math.round(a.apr*100)}%</td><td class="small">${a.open?"open":Math.round((a.last_seen-a.first_seen)/60)+" min"}</td></tr>`).join("")+"</table>"
  :`<div class="empty">No gaps found yet.</div>`;
 const kinds=[["all","All"],["news","News"],["sports","Sports"],["momentum","Momentum"],["favourite","Favourites"],["arb","Arbitrage"],["mover","Price moves"]];
 $("chips").innerHTML=kinds.map(([k,l])=>`<span class="chip ${filter===k?"on":""}" data-k="${k}">${l}${k!=="all"&&st.signals[k]?" "+st.signals[k]:""}</span>`).join("");
 document.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{filter=c.dataset.k;render()});
 const sg=S.signals.filter(s=>filter==="all"||s.kind===filter).slice(0,60);
 $("sigs").innerHTML=sg.length?sg.map(s=>{const d=s.data||{};let extra="";
  if(s.kind==="news")extra=`${esc(d.source)} · ${d.age_min}m old · market ${d.yes}`+(d.p_yes!=null?` · AI says ${d.p_yes} (conf ${d.conf})`:"")+(d.reason?`<br><i>${esc(d.reason)}</i>`:"");
  if(s.kind==="sports")extra=`${esc(d.sport||"")} · ${d.books} bookmakers`;
  if(s.kind==="arb")extra=`${d.profit_c}c per set · up to $${d.max_usd} · ${d.days} days · ${Math.round(d.apr*100)}%/yr`;
  return`<div class="sig"><div class="top"><span class="tag t-${s.kind}">${s.kind}</span><a class="t" href="${esc(s.url)}" target="_blank">${esc(s.title)}</a><span class="sp"></span><span class="small muted">${ago(s.ts)}</span></div>
  <div class="d small">${esc(s.detail)}</div>${extra?`<div class="small muted">${extra}</div>`:""}<div class="a ${String(s.action).includes("buy")?"act-buy":"muted"}">${esc(s.action||"")}</div></div>`}).join("")
  :`<div class="empty">No signals yet. News is checked every minute; arbitrage and price moves every 5 minutes (price moves need an hour of history first).</div>`;
 const L=S.limits;
 $("limits").innerHTML=`<table>
 <tr><td>Trades today</td><td class="r">${S.trades_today} / ${L.MAX_TRADES_PER_DAY}</td></tr>
 <tr><td>Exposure</td><td class="r">$${m.open_cost.toFixed(0)} / $${L.MAX_EXPOSURE}</td></tr>
 <tr><td>Open positions</td><td class="r">${S.open.length} / ${L.MAX_OPEN_POSITIONS}</td></tr>
 <tr><td>Today's loss before auto-pause</td><td class="r">${usd(S.today_pnl,1)} / -$${L.DAILY_LOSS_LIMIT}</td></tr>
 <tr><td>Max per trade · Kelly fraction</td><td class="r">$${L.MAX_STAKE} · ${L.KELLY_FRACTION}</td></tr>
 <tr><td>Min edge to trade</td><td class="r">${Math.round(L.MIN_EDGE*100)} points</td></tr>
 <tr><td>Min edge at ${Math.round(L.HIGH_PRICE_ZONE*100)}c+ (max ${Math.round(L.MAX_PRICE*100)}c)</td><td class="r">${Math.round(L.MIN_EDGE_HIGH*100)} points</td></tr></table>`;
 $("log").innerHTML=S.logs.map(l=>`<div class="${l.level}">${new Date(l.ts*1e3).toLocaleTimeString()} ${esc(l.msg)}</div>`).join("")||`<div class="muted">–</div>`;
}
function chart(){
 const pts=S.equity, el=$("chart"), W=el.clientWidth||600, H=200, p={l:52,r:10,t:10,b:24};
 if(pts.length<2){el.innerHTML=`<div class="empty">The equity line appears after a few minutes of running.</div>`;return}
 const xs=pts.map(d=>d[0]), ys=pts.map(d=>d[1]).concat([S.limits.BANKROLL]);
 let y0=Math.min(...ys), y1=Math.max(...ys); if(y1-y0<2){y0-=1;y1+=1} const pad=(y1-y0)*.1; y0-=pad; y1+=pad;
 const X=t=>p.l+(t-xs[0])/(xs[xs.length-1]-xs[0]||1)*(W-p.l-p.r), Y=v=>p.t+(1-(v-y0)/(y1-y0))*(H-p.t-p.b);
 const line=pts.map((d,i)=>(i?"L":"M")+X(d[0]).toFixed(1)+","+Y(d[1]).toFixed(1)).join("");
 const up=pts[pts.length-1][1]>=S.limits.BANKROLL, col=up?"var(--good)":"var(--bad)";
 let grid=""; for(let i=0;i<=3;i++){const v=y0+(y1-y0)*i/3;grid+=`<line x1="${p.l}" x2="${W-p.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${p.l-6}" y="${Y(v)+4}" text-anchor="end">$${v.toFixed(0)}</text>`}
 const base=Y(S.limits.BANKROLL);
 el.innerHTML=`<svg width="${W}" height="${H}" role="img" aria-label="Equity over time">${grid}
 <line x1="${p.l}" x2="${W-p.r}" y1="${base}" y2="${base}" stroke="var(--mute)" stroke-dasharray="3 4"/>
 <path d="${line} L${X(xs[xs.length-1])},${H-p.b} L${X(xs[0])},${H-p.b}Z" fill="${col}" opacity=".08"/>
 <path d="${line}" fill="none" stroke="${col}" stroke-width="2"/>
 <text x="${p.l}" y="${H-6}">${tm(xs[0])}</text><text x="${W-p.r}" y="${H-6}" text-anchor="end">${tm(xs[xs.length-1])}</text></svg>`;
}
addEventListener("resize",()=>S&&chart());
load();if(!window.STATIC_STATE)setInterval(load,5000);
</script></body></html>"""
