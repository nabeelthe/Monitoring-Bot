"""Web dashboard + observability endpoints.

  GET /            dark-mode live dashboard (timeline, risk panel, monitor health)
  GET /api/overview  stats + risk + predictions + monitor health (JSON)
  GET /api/events    recent events (JSON), ?hours=24&q=search
  GET /healthz       liveness probe
  GET /metrics       Prometheus text format
"""

import json
import logging
import time

from aiohttp import web

from . import __version__

log = logging.getLogger("nuva.dashboard")

PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nuva Intelligence</title><style>
:root{--bg:#0b0e14;--panel:#131722;--line:#1f2633;--text:#d6dbe5;--dim:#7a8494;
--red:#f2555a;--orange:#f0883e;--yellow:#e3c04c;--green:#4cc38a;--blue:#539bf5}
*{box-sizing:border-box;margin:0}body{background:var(--bg);color:var(--text);
font:14px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;padding:18px;max-width:1200px;margin:auto}
h1{font-size:17px;letter-spacing:.06em}h1 span{color:var(--green)}
h2{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-top:14px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin-top:10px}
.stat b{font-size:20px;display:block}.stat span{color:var(--dim);font-size:11px}
.ev{display:flex;gap:8px;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px}
.ev:last-child{border:none}.ev .t{color:var(--dim);white-space:nowrap;font-variant-numeric:tabular-nums}
.tag{font-size:10px;padding:1px 7px;border-radius:9px;background:var(--line);color:var(--dim);white-space:nowrap}
.p-critical{color:var(--red)}.p-high{color:var(--orange)}.p-medium{color:var(--yellow)}.p-low{color:var(--dim)}
.risk{margin-bottom:9px}.risk .bar{height:5px;background:var(--line);border-radius:3px;margin-top:3px}
.risk .fill{height:5px;border-radius:3px;background:var(--green)}
.risk .fill.warn{background:var(--yellow)}.risk .fill.bad{background:var(--red)}
.risk small{color:var(--dim)}
.mon{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;color:var(--dim)}
.dot{color:var(--green)}.dot.bad{color:var(--red)}.dot.warn{color:var(--yellow)}
.pred{font-size:12.5px;padding:4px 0;color:var(--dim)}.pred b{color:var(--text)}
input{background:var(--bg);border:1px solid var(--line);color:var(--text);border-radius:7px;
padding:6px 10px;width:100%;margin-bottom:8px;font:inherit}
footer{color:var(--dim);font-size:11px;margin-top:16px}
</style></head><body>
<h1>⛓ NUVA <span>INTELLIGENCE</span> <span style="float:right;font-size:11px;color:var(--dim)" id="clock"></span></h1>
<div class="stats" id="stats"></div>
<div class="grid">
 <div>
  <div class="panel" style="margin-bottom:14px"><h2>HASH — 24h <span id="pxnow" style="float:right;color:var(--green)"></span></h2>
   <canvas id="chart" height="120" style="width:100%"></canvas></div>
  <div class="panel"><h2>Signal timeline</h2>
   <input id="q" placeholder="Search events… (title/body)">
   <div id="events"></div></div>
 </div>
 <div>
  <div class="panel" style="margin-bottom:14px"><h2>Risk panel</h2><div id="risk"></div></div>
  <div class="panel" style="margin-bottom:14px"><h2>Predictions</h2><div id="preds"></div></div>
  <div class="panel"><h2>Collectors</h2><div id="mons"></div></div>
 </div>
</div>
<footer id="foot"></footer>
<script>
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function drawChart(){
 try{
  const d=await (await fetch('api/ticks?hours=24')).json();
  const ticks=d.ticks||[]; const cv=document.getElementById('chart');
  const ctx=cv.getContext('2d'); const W=cv.width=cv.clientWidth*2, H=cv.height=240;
  ctx.clearRect(0,0,W,H);
  if(ticks.length<2){ctx.fillStyle='#7a8494';ctx.font='24px sans-serif';
   ctx.fillText('collecting price history…',20,H/2);return}
  const ps=ticks.map(t=>t.price), lo=Math.min(...ps), hi=Math.max(...ps), pad=(hi-lo)||1;
  const X=i=>i/(ticks.length-1)*(W-8)+4, Y=p=>H-12-((p-lo)/pad)*(H-24);
  // volume bars
  const vs=ticks.map(t=>t.volume||0), vmax=Math.max(...vs)||1;
  ctx.fillStyle='rgba(83,155,245,.25)';
  ticks.forEach((t,i)=>{const h=(vs[i]/vmax)*(H*0.25);ctx.fillRect(X(i)-1,H-h,2,h)});
  // price line
  ctx.beginPath();ctx.strokeStyle='#4cc38a';ctx.lineWidth=3;
  ticks.forEach((t,i)=>{i?ctx.lineTo(X(i),Y(t.price)):ctx.moveTo(X(i),Y(t.price))});
  ctx.stroke();
  const last=ps[ps.length-1], first=ps[0], chg=first?((last-first)/first*100):0;
  document.getElementById('pxnow').textContent=`$${last.toFixed(6)} (${chg>=0?'+':''}${chg.toFixed(2)}%)`;
  document.getElementById('pxnow').style.color=chg>=0?'var(--green)':'var(--red)';
 }catch(e){}}

async function load(){
 try{
  const o=await (await fetch('api/overview')).json();
  const q=document.getElementById('q').value.trim();
  const ev=await (await fetch('api/events?hours=48&limit=60'+(q?'&q='+encodeURIComponent(q):''))).json();
  document.getElementById('stats').innerHTML=
   `<div class="stat"><b>${o.stats.total_events}</b><span>events stored</span></div>
    <div class="stat"><b>${o.stats.events_24h}</b><span>signals 24h</span></div>
    <div class="stat"><b class="p-critical">${o.stats.critical_24h}</b><span>critical 24h</span></div>
    <div class="stat"><b>${o.stats.alerts_sent}</b><span>alerts sent</span></div>
    <div class="stat"><b>${o.stats.ai_briefs}</b><span>AI briefs</span></div>
    <div class="stat"><b>${o.stats.uptime}</b><span>uptime</span></div>`;
  document.getElementById('events').innerHTML=ev.events.map(e=>{
   const d=new Date(e.ts*1000).toISOString().slice(5,16).replace('T',' ');
   return `<div class="ev"><span class="t">${d}</span><span class="tag">${esc(e.layer)}</span>
    <span class="p-${esc(e.priority)}">●</span>
    <span>${e.url?`<a style="color:inherit" href="${esc(e.url)}" target="_blank">${esc(e.title)}</a>`:esc(e.title)}
    <small style="color:var(--dim)"> ${e.confidence}%</small></span></div>`}).join('')||'<div class="pred">no events yet</div>';
  document.getElementById('risk').innerHTML=o.risk.map(r=>{
   const cls=r.score>=60?'bad':(r.score>=35?'warn':'');
   const ar={rising:'↑',falling:'↓',flat:'→'}[r.trend];
   return `<div class="risk"><div>${esc(r.name)} <b style="float:right">${r.score} ${ar}</b></div>
    <div class="bar"><div class="fill ${cls}" style="width:${r.score}%"></div></div>
    <small>${esc(r.detail)}</small></div>`}).join('');
  document.getElementById('preds').innerHTML=o.predictions.map(p=>
   `<div class="pred"><b>${p.probability}%</b> ${esc(p.name)}</div>`).join('');
  document.getElementById('mons').innerHTML=o.monitors.map(m=>{
   const cls=m.failures>=5?'bad':(m.failures>0?'warn':'');
   return `<div class="mon"><span><span class="dot ${cls}">●</span> ${esc(m.name)}</span><span>${m.last_ok}</span></div>`}).join('');
  document.getElementById('foot').textContent=`nuva-intel v${o.version} · refreshed ${new Date().toLocaleTimeString()}`;
 }catch(e){document.getElementById('foot').textContent='refresh failed: '+e}}
document.getElementById('q').addEventListener('input',()=>{clearTimeout(window._t);window._t=setTimeout(load,350)});
setInterval(()=>{document.getElementById('clock').textContent=new Date().toUTCString().slice(17,25)+' UTC'},1000);
load();drawChart();setInterval(load,30000);setInterval(drawChart,60000);
</script></body></html>"""


def _fmt_ago(ts: float) -> str:
    if not ts:
        return "never"
    d = int(time.time() - ts)
    return f"{d}s" if d < 120 else (f"{d // 60}m" if d < 7200 else f"{d // 3600}h")


class Dashboard:
    def __init__(self, config, pipeline, scheduler, state):
        self.config = config
        self.pipeline = pipeline
        self.scheduler = scheduler
        self.state = state
        self.port = config.getint("dashboard.port", 8088)
        self.host = str(config.get("dashboard.host", "0.0.0.0"))
        self._runner: web.AppRunner | None = None
        self.started_at = time.time()

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/overview", self._overview)
        app.router.add_get("/api/events", self._events)
        app.router.add_get("/api/ticks", self._ticks)
        app.router.add_get("/healthz", self._health)
        app.router.add_get("/metrics", self._metrics)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("dashboard listening on http://%s:%s", self.host, self.port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    # ---- handlers ---------------------------------------------------------
    async def _index(self, _request):
        return web.Response(text=PAGE, content_type="text/html")

    async def _overview(self, _request):
        p = self.pipeline
        prio = p.store.counts_by_priority(24)
        up = int(time.time() - self.scheduler.started_at)
        data = {
            "version": __version__,
            "stats": {
                "total_events": p.store.total(),
                "events_24h": sum(p.store.counts_by_layer(24).values()),
                "critical_24h": prio.get("critical", 0),
                "alerts_sent": p.counters["sent"],
                "ai_briefs": p.counters["ai_briefs"],
                "uptime": f"{up // 3600}h{(up % 3600) // 60:02d}m",
            },
            "risk": [vars(d) for d in p.risk.snapshot(self.scheduler.statuses)],
            "predictions": [
                {"name": x.name, "probability": x.probability, "horizon": x.horizon}
                for x in p.predictor.all()
            ],
            "monitors": [
                {"name": st.monitor.name, "failures": st.consecutive_failures,
                 "last_ok": _fmt_ago(st.last_ok), "alerts": st.alerts_sent}
                for st in self.scheduler.statuses.values()
            ],
        }
        return web.json_response(data)

    async def _events(self, request):
        try:
            hours = float(request.query.get("hours", 24))
            limit = min(int(request.query.get("limit", 100)), 500)
        except ValueError:
            hours, limit = 24.0, 100
        q = request.query.get("q", "").strip()
        if q:
            events = self.pipeline.store.search(q, limit=limit)
        else:
            events = self.pipeline.store.recent(hours, limit=limit)
        return web.json_response({"events": [e.to_dict() for e in events]})

    async def _ticks(self, request):
        try:
            hours = min(float(request.query.get("hours", 24)), 24 * 90)
        except ValueError:
            hours = 24.0
        rows = self.pipeline.store.ticks(hours)
        return web.json_response({"ticks": [{"ts": t, "price": p, "volume": v} for t, p, v in rows]})

    async def _health(self, _request):
        failing = sum(1 for st in self.scheduler.statuses.values() if st.consecutive_failures >= 5)
        healthy = failing < max(len(self.scheduler.statuses), 1)
        return web.json_response(
            {"status": "ok" if healthy else "degraded", "failing_monitors": failing},
            status=200 if healthy else 503,
        )

    async def _metrics(self, _request):
        p = self.pipeline
        lines = [
            "# TYPE nuva_events_total counter",
            f"nuva_events_total {p.counters['events']}",
            "# TYPE nuva_alerts_sent_total counter",
            f"nuva_alerts_sent_total {p.counters['sent']}",
            "# TYPE nuva_events_digested_total counter",
            f"nuva_events_digested_total {p.counters['digested']}",
            "# TYPE nuva_ai_briefs_total counter",
            f"nuva_ai_briefs_total {p.counters['ai_briefs']}",
            "# TYPE nuva_uptime_seconds gauge",
            f"nuva_uptime_seconds {int(time.time() - self.scheduler.started_at)}",
            "# TYPE nuva_monitor_consecutive_failures gauge",
        ]
        for st in self.scheduler.statuses.values():
            name = st.monitor.name.replace('"', "'")
            lines.append(f'nuva_monitor_consecutive_failures{{monitor="{name}"}} {st.consecutive_failures}')
        lines.append("# TYPE nuva_risk_score gauge")
        for d in p.risk.snapshot(self.scheduler.statuses):
            lines.append(f'nuva_risk_score{{dimension="{d.name}"}} {d.score}')
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")
