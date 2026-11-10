"""Render a standalone, read-only dashboard from the registered V2.1 evaluation."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

LANES = ("S5", "IMPULSE", "COMBINED")


def build(report, tape):
    result = json.loads(report.read_bytes())
    raw = tape.read_bytes()
    if hashlib.sha256(raw).hexdigest() != result["tapes_sha256"]:
        raise ValueError("Evaluation tape does not match the report")
    data = json.loads(gzip.decompress(raw))
    views = {}
    for frame, summary in result["frames"].items():
        stats = summary["trade_metrics"]
        views[frame] = {
            "metrics": {lane: {
                "count": stats[lane]["closed_campaigns"]["count"],
                "losses": stats[lane]["closed_campaigns"]["losses"],
                "net": stats[lane]["closed_campaigns"]["net_cash"],
                "stressed": stats[lane]["stressed_campaigns"]["net_cash"],
                "dd": stats[lane]["campaign_close_drawdown_usd"],
                "pf": stats[lane]["closed_campaigns"]["profit_factor"],
            } for lane in LANES},
            "events": [
                {"at": e["start"], "side": e["side"], "movement": e["directional_extreme_distance"],
                 "lanes": {lane: {k: e["matches"][lane].get(k) for k in (
                     "watch_minutes", "confirmation_minutes", "fill_minutes", "fills",
                     "first_fill_engine", "miss", "capture_fraction")}
                           for lane in LANES}}
                for e in data[frame]["events"]
            ],
        }
    return HTML.replace("__V21_EVIDENCE__", json.dumps(views, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c"))


HTML = r"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QIRVANTA V2.1 · Event research</title>
<style>
  :root{color-scheme:dark;background:#0a111b;color:#e7edf5;font:15px/1.5 system-ui,sans-serif}
  body{margin:0 auto;padding:24px;max-width:1180px} h1{margin:0;font-size:clamp(24px,4vw,40px)}
  h2{font-size:1.15rem;margin-top:30px} p{max-width:80ch;color:#bbc8da} strong{color:#eef4fc}
  .tag{color:#52ddc8;font-weight:650;letter-spacing:.09em}.controls{display:flex;gap:16px;flex-wrap:wrap;margin:18px 0}
  select{padding:5px 10px;border:1px solid #43566e;background:#142134;border-radius:6px;color:inherit}
  .wrap{overflow:auto;border:1px solid #34455a;border-radius:10px}
  table{border-collapse:collapse;width:100%;min-width:710px}caption{text-align:left;padding:10px 12px;color:#c7d4e6}
  th,td{border-bottom:1px solid #263448;text-align:left;padding:8px 12px;vertical-align:top}th{background:#19273b}
  tr:nth-child(even){background:#111c2a}.down{color:#ff8698}.up{color:#64dfc7}small{color:#a3b3c9}a{color:#7ee3d1}
</style>
<main>
 <span class="tag">QIRVANTA / EXPERIMENTAL</span>
 <h1>Impulse coverage versus S5</h1>
 <p>Registered OANDA:XAUUSD archive. S5 uses observed native admissions. Impulse is a causal replay, and Combined is a single-slot replay of the two tapes. Each lane has its own result. The combined lane cannot recover S5 proposals absent from the original journal.</p>
 <div class="controls"><label>Timeframe <select id="frame"><option value="5">M5</option><option value="15">M15</option><option value="30">M30</option></select></label>
 <label>Event side <select id="side"><option value="0">Both</option><option value="1">BUY</option><option value="-1">SELL</option></select></label>
 <label>Outcome <select id="outcome"><option value="all">All events</option><option value="filled">Any lane filled</option><option value="missed">All lanes missed</option></select></label></div>
 <div class="wrap"><table><caption>Closed campaigns · both directions · USD</caption><thead><tr><th scope="col">Lane</th><th scope="col">Closed / losses</th><th scope="col">Net</th><th scope="col">Stressed net</th><th scope="col">PF</th><th scope="col">Campaign-close drawdown</th></tr></thead><tbody id="metrics"></tbody></table></div>
 <h2>Registered directional events</h2>
 <p id="count" role="status"></p>
 <div class="wrap"><table><caption>UTC hourly events · latency from event start · capture is favorable path, not profit</caption><thead><tr><th scope="col">Start UTC</th><th scope="col">Side / price distance</th><th scope="col">S5 observed</th><th scope="col">Impulse replay</th><th scope="col">Combined replay</th></tr></thead><tbody id="events"></tbody></table></div>
 <p>Source: <a href="V21_EVENT_EVALUATION.md">registered evaluation</a>. The Nov 10 diagnosis and the separate short native validation are reported outside this archive comparison.</p>
</main>
<script type="application/json" id="evidence">__V21_EVIDENCE__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById("evidence").textContent);
const lanes=["S5","IMPULSE","COMBINED"];
const labels={S5:"S5 · native archive",IMPULSE:"Impulse · replay",COMBINED:"Combined · one slot"};
const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>n==null?"—":Number(n).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
function row(values,parent){
  const tr=document.createElement("tr");
  for(const value of values){const td=document.createElement("td");td.textContent=value;tr.append(td);}
  parent.append(tr);return tr;
}
function view(e){
  if(e.fills){
    const cap=e.capture_fraction==null?"":" · "+fmt(100*e.capture_fraction,1)+"% capture";
    const who=e.first_fill_engine==null?"":" · "+e.first_fill_engine;
    return e.fills+" fill"+(e.fills===1?"":"s")+" at "+fmt(e.fill_minutes,0)+"m"+who+cap;
  }
  const marks=[e.watch_minutes==null?null:"watch "+fmt(e.watch_minutes,0)+"m",
               e.confirmation_minutes==null?null:"confirmed "+fmt(e.confirmation_minutes,0)+"m"].filter(Boolean);
  return [String(e.miss||"NO FILL").toLowerCase().replaceAll("_"," "),...marks].join(" · ");
}
function render(){
  const frame=data[$("frame").value], side=Number($("side").value), outcome=$("outcome").value;
  $("metrics").replaceChildren();$("events").replaceChildren();
  for(const lane of lanes){const m=frame.metrics[lane];const tr=row([labels[lane],m.count+" / "+m.losses,fmt(m.net),fmt(m.stressed),fmt(m.pf,3),fmt(m.dd)],$("metrics"));tr.cells[2].className=m.net<0?"down":"up";tr.cells[3].className=m.stressed<0?"down":"up";}
  let count=0;
  for(const e of frame.events){
    const any=lanes.some(k=>e.lanes[k].fills>0);
    if((side&&side!==e.side)||(outcome==="filled"&&!any)||(outcome==="missed"&&any))continue;
    const tr=row([new Date(e.at).toISOString().slice(0,16).replace("T"," "), (e.side>0?"BUY":"SELL")+" · "+fmt(e.movement,3),...lanes.map(k=>view(e.lanes[k]))],$("events"));
    tr.cells[1].className=e.side>0?"up":"down";count++;
  }
  $("count").textContent=count+" of "+frame.events.length+" events shown. Trade metrics above use all campaigns on the selected timeframe.";
}
for(const id of ["frame","side","outcome"])$(id).addEventListener("change",render);
render();
</script></html>
"""


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--report", type=Path, required=True)
    cli.add_argument("--tape", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    with args.out.open("x", encoding="utf-8", newline="\n") as file:
        file.write(build(args.report, args.tape))


if __name__ == "__main__":
    main()
