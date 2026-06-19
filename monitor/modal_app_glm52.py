"""CeoBench GLM-5.2 Monitor — Modal-deployed dashboard.

Reads run data from a Modal volume periodically updated by push_data.py.

Deploy: cd projects/saas-bench && modal deploy monitor/modal_app.py
"""

import json
import modal

app = modal.App("ceobench-glm52-dashboard")
volume = modal.Volume.from_name("bossbench-glm52-monitor-data", create_if_missing=True)
image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]")


@app.function(
    image=image,
    volumes={"/data": volume},
    cpu=0.25,
    memory=256,
    scaledown_window=900,
)
@modal.asgi_app()
def dashboard():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from pathlib import Path

    web = FastAPI()
    DATA_FILE = Path("/data/data.json")

    @web.get("/api/data")
    def get_data():
        try:
            volume.reload()
        except Exception:
            pass
        if DATA_FILE.exists():
            with open(DATA_FILE) as f:
                return json.load(f)
        return {"runs": [], "timestamp": None}

    @web.get("/", response_class=HTMLResponse)
    def index():
        return DASHBOARD_HTML

    return web


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CeoBench GLM-5.2 Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--text2:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#bc8cff}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text)}
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.header h1{font-size:18px;font-weight:600;white-space:nowrap}
.header select{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 12px;font-size:14px;min-width:260px}
.header .info{margin-left:auto;font-size:12px;color:var(--text2);display:flex;align-items:center;gap:8px}
.header .dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block}
.container{max-width:1400px;margin:0 auto;padding:16px 24px}
.run-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px;cursor:pointer;transition:border-color .2s}
.run-card:hover,.run-card.active{border-color:var(--accent)}
.run-card .label{font-weight:600;font-size:14px;margin-bottom:4px}
.run-card .meta{font-size:12px;color:var(--text2);margin-bottom:8px}
.run-card .stats{display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:13px}
.run-card .stats .val{font-weight:600}
.run-card .stats .cash{color:var(--green)}
.run-card .stats .divs{color:var(--yellow)}
.run-card .progress-bar{margin-top:8px;background:var(--bg);border-radius:4px;height:6px;overflow:hidden}
.run-card .progress-fill{background:var(--accent);height:100%;border-radius:4px;transition:width .3s}
.run-card .progress-text{font-size:11px;color:var(--text2);margin-top:2px}
.run-card .recent-actions{margin-top:8px;border-top:1px solid var(--border);padding-top:6px}
.run-card .ra-item{display:flex;align-items:baseline;gap:6px;font-size:11px;padding:2px 0;line-height:1.4}
.run-card .ra-tool{color:var(--purple);font-weight:600;white-space:nowrap}
.run-card .ra-args{color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:10px}
.run-card .ra-time{color:var(--text2);white-space:nowrap;font-size:10px;margin-left:auto}
.detail{display:none}.detail.visible{display:block}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.chart-box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
.chart-box h3{font-size:13px;color:var(--text2);margin-bottom:8px}
.chart-box canvas{max-height:220px}
.tabs{display:flex;gap:0;flex-wrap:wrap}
.tab{padding:8px 20px;font-size:13px;font-weight:500;cursor:pointer;border:1px solid var(--border);border-bottom:none;background:var(--bg);color:var(--text2);border-radius:6px 6px 0 0}
.tab.active{background:var(--surface);color:var(--text)}
.tab-content{background:var(--surface);border:1px solid var(--border);border-radius:0 8px 8px 8px;display:none}
.tab-content.active{display:block}
.action-list{max-height:600px;overflow-y:auto}
.action-item{padding:10px 16px;border-bottom:1px solid var(--border);font-size:13px}
.action-item:last-child{border-bottom:none}
.action-item .action-header{display:flex;gap:12px;align-items:baseline;margin-bottom:4px;flex-wrap:wrap}
.day-badge{background:var(--accent);color:#000;font-size:11px;font-weight:700;padding:1px 6px;border-radius:3px}
.turn-badge{background:var(--border);font-size:11px;padding:1px 6px;border-radius:3px}
.tool-name{font-weight:600;color:var(--purple)}
.timestamp{color:var(--text2);font-size:11px;margin-left:auto}
.args{color:var(--text2);font-size:12px;margin:2px 0;font-family:'SF Mono',Monaco,Consolas,monospace}
.result-toggle{color:var(--accent);cursor:pointer;font-size:12px;user-select:none}
.result-content{display:none;margin-top:6px;background:var(--bg);border-radius:6px;padding:12px;font-size:12px;font-family:'SF Mono',Monaco,Consolas,monospace;white-space:pre-wrap;word-break:break-word;max-height:500px;overflow-y:auto;line-height:1.5}
.result-content.visible{display:block}
.response-card{padding:14px 16px;border-bottom:1px solid var(--border)}
.response-card:last-child{border-bottom:none}
.response-card .resp-header{display:flex;gap:12px;align-items:baseline;margin-bottom:6px}
.response-card .content-block{background:var(--bg);border-radius:6px;padding:12px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;max-height:600px;overflow-y:auto}
.response-card .tc-item{background:var(--bg);border-radius:6px;padding:8px 12px;margin-top:4px;font-size:12px}
.response-card .tc-item .tc-name{color:var(--purple);font-weight:600}
.response-card .tc-item pre{margin-top:4px;font-family:'SF Mono',Monaco,Consolas,monospace;white-space:pre-wrap;color:var(--text2)}
.loading{text-align:center;padding:40px;color:var(--text2)}
.compare-table{width:100%;border-collapse:collapse;font-size:13px}
.compare-table th,.compare-table td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}
.compare-table th{color:var(--text2);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.compare-table td.num{text-align:right;font-family:'SF Mono',Monaco,Consolas,monospace}
.compare-table tr:hover{background:rgba(88,166,255,.05)}
.social-post{padding:12px 16px;border-bottom:1px solid var(--border)}
.social-post:last-child{border-bottom:none}
.social-post .post-meta{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text2);margin-bottom:4px;flex-wrap:wrap}
.social-post .post-content{font-size:13px;line-height:1.5}
.social-post .post-reply{margin-top:6px;margin-left:16px;padding:8px 12px;background:var(--bg);border-radius:6px;border-left:3px solid var(--border);font-size:12px}
.social-post .post-reply.positive{border-left-color:var(--green)}
.social-post .post-reply.negative{border-left-color:var(--red)}
.score-badge{display:inline-block;font-size:10px;font-weight:700;padding:1px 5px;border-radius:3px;font-family:'SF Mono',Monaco,Consolas,monospace}
.score-positive{background:rgba(63,185,80,.2);color:var(--green)}
.score-negative{background:rgba(248,81,73,.2);color:var(--red)}
.score-neutral{background:var(--border);color:var(--text2)}
.discovery-table{width:100%;border-collapse:collapse;font-size:13px}
.discovery-table th,.discovery-table td{padding:8px 14px;text-align:left;border-bottom:1px solid var(--border)}
.discovery-table th{color:var(--text2);font-weight:500;font-size:12px;text-transform:uppercase}
.disc-yes{color:var(--green);font-weight:600}
.disc-no{color:var(--text2)}
@media(max-width:800px){.charts{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
  <h1>CeoBench GLM-5.2 Monitor</h1>
  <select id="runSelect" onchange="selectRun(this.value)"><option value="">— All Runs Overview —</option></select>
  <div class="info"><span class="dot"></span> Auto-refresh 15s <span id="lastUpdate"></span> <span id="dataAge" style="color:var(--yellow)"></span></div>
</div>
<div class="container">
  <div id="overviewSection">
    <div style="margin-bottom:16px">
      <h2 style="font-size:16px;color:var(--text2);margin-bottom:8px">Compare All Runs</h2>
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:auto">
        <table class="compare-table"><thead><tr>
          <th>Run</th><th>Model</th><th>Day</th><th>Progress</th>
          <th style="text-align:right">Cash</th><th style="text-align:right">Subscribers</th>
          <th style="text-align:right">Wk. Profit</th><th style="text-align:right">F. Dividends</th>
          <th style="text-align:right">Turns</th><th>Last Action</th>
        </tr></thead><tbody id="compareBody"></tbody></table>
      </div>
    </div>
    <div class="overview" id="runCards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px"></div>
  </div>
  <div id="detailSection" class="detail">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <button onclick="selectRun('')" style="background:var(--surface);border:1px solid var(--border);color:var(--accent);padding:4px 12px;border-radius:6px;cursor:pointer;font-size:13px">&#8592; Back</button>
      <h2 id="detailTitle" style="margin:0"></h2>
    </div>
    <div class="overview" id="detailStats" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px"></div>

    <!-- Group Discovery Status -->
    <div id="discoverySection" style="margin-bottom:20px"></div>

    <!-- Core charts: Cash, Subscribers -->
    <div class="charts">
      <div class="chart-box"><h3>Cash Balance</h3><canvas id="cashChart"></canvas></div>
      <div class="chart-box"><h3>Individual Subs + Enterprise Seats</h3><canvas id="subsChart"></canvas></div>
    </div>
    <!-- Dividends + Weekly Profit -->
    <div class="charts">
      <div class="chart-box"><h3>Founder Dividends (Cumulative)</h3><canvas id="divChart"></canvas></div>
      <div class="chart-box"><h3>Weekly Profit (7-day windows)</h3><canvas id="profitChart"></canvas></div>
    </div>
    <!-- Cash prediction % error (1wk / 4wk / 12wk on one plot) -->
    <div class="charts">
      <div class="chart-box" style="grid-column:1/-1"><h3>Cash Prediction % Error by Horizon (1wk / 4wk / 12wk / 26wk)</h3><canvas id="predictionChart" style="max-height:280px"></canvas></div>
    </div>
    <!-- Per-horizon: predicted cash + 95% CI band + actual cash -->
    <div class="charts">
      <div class="chart-box"><h3>Cash Forecast: 1 week (+7d) — predicted ± 95% CI vs actual</h3><canvas id="predCashH7" style="max-height:240px"></canvas></div>
      <div class="chart-box"><h3>Cash Forecast: 4 weeks (+28d) — predicted ± 95% CI vs actual</h3><canvas id="predCashH28" style="max-height:240px"></canvas></div>
    </div>
    <div class="charts">
      <div class="chart-box"><h3>Cash Forecast: 12 weeks (+84d) — predicted ± 95% CI vs actual</h3><canvas id="predCashH84" style="max-height:240px"></canvas></div>
      <div class="chart-box"><h3>Cash Forecast: 26 weeks (+182d) — predicted ± 95% CI vs actual</h3><canvas id="predCashH182" style="max-height:240px"></canvas></div>
    </div>
    <!-- Timing -->
    <div class="charts">
      <div class="chart-box" id="timingChartBox"><h3>Day Time Breakdown (s)</h3><canvas id="timingChart"></canvas></div>
    </div>
    <!-- Reputation + Q_min -->
    <div class="charts">
      <div class="chart-box"><h3>Reputation by Group</h3><canvas id="reputationChart"></canvas></div>
      <div class="chart-box"><h3>Effective Q_min (New Leads) by Group</h3><canvas id="qminChart"></canvas></div>
    </div>
    <!-- Per-group q_bias drift only (excludes global) -->
    <div class="charts">
      <div class="chart-box" style="grid-column:1/-1"><h3>Per-Group q_bias Drift Only (excludes global; isolates group reactivity to competitor shocks)</h3><canvas id="qminDriftOnlyChart" style="max-height:280px"></canvas></div>
    </div>
    <!-- Quality -->
    <div class="charts">
      <div class="chart-box" style="grid-column:1/-1"><h3>Delivered Quality by Group × Plan</h3><canvas id="qualityChart" style="max-height:280px"></canvas></div>
    </div>
    <!-- Ads Revenue -->
    <div class="charts">
      <div class="chart-box" style="grid-column:1/-1"><h3>Ads Revenue per Active Customer Group</h3><canvas id="adsRevenueChart" style="max-height:280px"></canvas></div>
    </div>
    <!-- Weekly churn rate per group -->
    <div class="charts">
      <div class="chart-box" style="grid-column:1/-1"><h3>Trailing 7-day Churn Rate by Customer Group (cancellations in past week ÷ subs active 7 days ago)</h3><canvas id="churnChart" style="max-height:280px"></canvas></div>
    </div>
    <!-- Time breakdown bars -->
    <div class="charts">
      <div class="chart-box"><h3>Time Breakdown</h3><div id="timingBars" style="padding:8px 0"></div></div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      <div class="tab active" onclick="switchTab('actions')">Tool Calls</div>
      <div class="tab" onclick="switchTab('responses')">LLM Responses</div>
      <div class="tab" onclick="switchTab('rationales')">Rationales</div>
      <div class="tab" onclick="switchTab('timing')">Timing</div>
      <div class="tab" onclick="switchTab('agentPosts')">Agent Posts</div>
      <div class="tab" onclick="switchTab('customerPosts')">Customer Posts</div>
    </div>
    <div id="actionsTab" class="tab-content active"><div class="action-list" id="actionList"><div class="loading">Loading...</div></div></div>
    <div id="responsesTab" class="tab-content"><div class="action-list" id="responseList"><div class="loading">Loading...</div></div></div>
    <div id="rationalesTab" class="tab-content"><div class="action-list" id="rationaleList"><div class="loading">Loading...</div></div></div>
    <div id="timingTab" class="tab-content"><div class="action-list" id="timingList"><div class="loading">Loading...</div></div></div>
    <div id="agentPostsTab" class="tab-content"><div class="action-list" id="agentPostList"><div class="loading">Loading...</div></div></div>
    <div id="customerPostsTab" class="tab-content"><div class="action-list" id="customerPostList"><div class="loading">Loading...</div></div></div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s),$$=s=>document.querySelectorAll(s);
let allData={runs:[]},currentRun=null,charts={};
const GROUP_COLORS={'S1':'#58a6ff','S2':'#3fb950','S3':'#bc8cff','E1':'#f0883e','E2':'#f85149','E3':'#d29922','S4':'#a5d6ff','E4':'#ff9bce','D_S01':'#00d4aa','D_S02':'#00b4d8','D_S03':'#48bfe3','D_S04':'#90e0ef','D_S05':'#caf0f8','D_S06':'#06d6a0','D_S07':'#1b9aaa','D_S08':'#05668d','D_S09':'#028090','D_S10':'#00a896','D_E01':'#e76f51','D_E02':'#f4a261','D_E03':'#e9c46a','D_E04':'#2a9d8f','D_E05':'#264653','D_E06':'#e63946','D_E07':'#a8dadc','D_E08':'#457b9d','D_E09':'#1d3557','D_E10':'#fca311'};

function fmt(n,p){p=p||'';if(n==null)return'\u2014';if(Math.abs(n)>=1e6)return p+(n/1e6).toFixed(2)+'M';if(Math.abs(n)>=1e3)return p+(n/1e3).toFixed(1)+'K';return p+n.toLocaleString()}
function fmtCash(n){return n==null?'\u2014':'$'+fmt(n)}
function pct(c,t){return t?Math.round(c/t*100):0}
function displayVal(v,f){return v==null?f:v}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function timeAgo(ts){if(!ts)return'';var s=Math.floor((Date.now()-new Date(ts))/1000);if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
function fmtResult(raw){if(!raw)return'(empty)';var s=String(raw);try{return esc(JSON.stringify(JSON.parse(s),null,2))}catch(e){}return esc(s)}
function fmtArgs(args){if(!args)return'';if(typeof args==='string')return esc(args);if(args.command)return esc(args.command);if(args.path)return esc(args.path);if(args.code)return esc(args.code.substring(0,200));return esc(JSON.stringify(args))}
var actEmoji={'bash':'\ud83d\udd27','read_file':'\ud83d\udcc2','write_file':'\u270d\ufe0f','edit_file':'\u270f\ufe0f','search_files':'\ud83d\udd0d','glob_files':'\ud83d\udd0d','_dashboard':'\ud83d\udcca','_reasoning':'\ud83d\udcad'};
function briefArgs(args){if(!args)return'';if(typeof args==='string')return args.substring(0,60);if(args.command){var c=args.command;return c.length>60?c.substring(0,57)+'...':c}if(args.path)return args.path;if(args.code)return args.code.substring(0,60);try{var s=JSON.stringify(args);return s.length>60?s.substring(0,57)+'...':s}catch(e){return''}}
function scoreBadge(v){if(v==null)return'';var cls=v>0.1?'score-positive':v<-0.1?'score-negative':'score-neutral';return'<span class="score-badge '+cls+'">'+v.toFixed(2)+'</span>'}

// --- UI state preservation across auto-refresh ---
// Track user interaction to pause refresh while reading
var _userInteracting=false,_interactTimer=null;
function _markInteraction(){_userInteracting=true;clearTimeout(_interactTimer);_interactTimer=setTimeout(function(){_userInteracting=false},60000)}
document.addEventListener('click',function(e){if(e.target.closest('.result-toggle')||e.target.closest('.action-item'))_markInteraction()});

function _actionId(a,i){return'd'+a.day+'t'+a.turn+'_'+a.tool+'_'+i}

function saveUIState(){
  var state={scrollY:window.scrollY,activeTab:null,expandedActions:[],listScrolls:{}};
  var at=document.querySelector('.tab.active');if(at)state.activeTab=at.textContent.trim();
  // Expanded action results by stable data-action-id
  document.querySelectorAll('#actionList .action-item').forEach(function(el){
    var rc=el.querySelector('.result-content');
    if(rc&&rc.classList.contains('visible')){
      var aid=el.getAttribute('data-action-id');if(aid)state.expandedActions.push(aid);
    }
  });
  ['actionList','responseList','timingList','agentPostList','customerPostList'].forEach(function(id){
    var el=document.getElementById(id);if(el)state.listScrolls[id]=el.scrollTop;
  });
  return state;
}
function restoreUIState(state){
  if(!state)return;
  // Restore expanded action results by stable ID
  if(state.expandedActions.length){
    var idSet={};state.expandedActions.forEach(function(id){idSet[id]=true});
    document.querySelectorAll('#actionList .action-item').forEach(function(el){
      var aid=el.getAttribute('data-action-id');
      if(aid&&idSet[aid]){
        var rc=el.querySelector('.result-content');
        var tog=el.querySelector('.result-toggle');
        if(rc)rc.classList.add('visible');
        if(tog)tog.textContent='\u25bc Hide result';
      }
    });
  }
  if(state.activeTab){
    var tabMap={'Tool Calls':'actions','LLM Responses':'responses','Rationales':'rationales','Timing':'timing','Agent Posts':'agentPosts','Customer Posts':'customerPosts'};
    var t=tabMap[state.activeTab];if(t)switchTab(t);
  }
  Object.keys(state.listScrolls).forEach(function(id){
    var el=document.getElementById(id);if(el)el.scrollTop=state.listScrolls[id];
  });
  window.scrollTo(0,state.scrollY);
}

async function fetchData(force){
  // Skip DOM re-render if user is actively reading (still fetch data silently)
  try{var r=await fetch('/api/data');var d=await r.json();
    if(d.runs)allData=d;
    $('#lastUpdate').textContent=new Date().toLocaleTimeString();
    if(d.timestamp){var age=Math.floor((Date.now()-new Date(d.timestamp))/1000);$('#dataAge').textContent=age>60?'(data '+Math.floor(age/60)+'m old)':''}
    // If user has expanded items and this isn't forced, skip re-render
    if(_userInteracting&&!force){$('#lastUpdate').textContent+=' (paused)';return}
    var uiState=saveUIState();renderOverview();if(currentRun)renderDetail();restoreUIState(uiState);
  }catch(e){console.error(e)}}

function renderOverview(){
  var runs=(allData.runs||[]).slice().sort(function(a,b){return (a.model||'').toLowerCase().localeCompare((b.model||'').toLowerCase())});var tbody='';
  for(var i=0;i<runs.length;i++){var r=runs[i];var p=pct(r.current_day||0,r.total_days||1095);
    var hb=r.last_heartbeat;var hbAge=hb?Math.floor((Date.now()-new Date(hb))/1000):null;var hbColor=hbAge!==null?(hbAge<60?'var(--green)':hbAge<300?'var(--yellow)':'var(--red)'):'var(--text2)';var hbDot='<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+hbColor+';margin-right:4px"></span>';
    var lastAct=(r.recent_activity||r.recent_actions||[])[0];var lastActHtml=hbDot+(hb?'<span style="font-size:11px;color:'+hbColor+'">'+timeAgo(hb)+'</span> ':'')+(lastAct?(lastAct.type==='llm'?'<span style="color:var(--accent);font-weight:600">\ud83e\udde0 LLM '+(lastAct.elapsed_s||'?')+'s</span>':'<span style="color:var(--purple);font-weight:600">'+(actEmoji[lastAct.tool]||'\u2699\ufe0f')+' '+esc(lastAct.tool)+'</span>'):'');
    var wp=r.weekly_profit;var wpCol=wp!=null?(wp>=0?'color:var(--green)':'color:var(--red)'):'';
    tbody+='<tr style="cursor:pointer" onclick="selectRun(\''+r.run_id+'\')"><td><strong>'+esc(r.label)+'</strong><br><span style="color:var(--text2);font-size:11px">'+r.run_id+'</span></td><td style="font-size:12px">'+esc(r.model||'')+'</td><td class="num">'+displayVal(r.current_day,'\u2014')+'</td><td><div style="display:flex;align-items:center;gap:8px"><div style="flex:1;background:var(--bg);border-radius:3px;height:4px"><div style="width:'+p+'%;background:var(--accent);height:100%;border-radius:3px"></div></div><span style="font-size:11px;color:var(--text2);min-width:35px">'+p+'%</span></div></td><td class="num" style="color:'+((r.cash||0)<0?'var(--red)':'var(--green)')+'">'+fmtCash(r.cash)+'</td><td class="num">'+fmt(r.subscribers)+'</td><td class="num" style="'+wpCol+'">'+(wp!=null?fmtCash(wp):'\u2014')+'</td><td class="num" style="color:var(--yellow)">'+fmtCash(r.founder_dividends)+'</td><td class="num">'+fmt(r.agent_turns)+'</td><td style="font-size:12px">'+lastActHtml+'</td></tr>'}
  $('#compareBody').innerHTML=tbody;
  var cards='';
  for(var i=0;i<runs.length;i++){var r=runs[i];var p=pct(r.current_day||0,r.total_days||1095);
    var raHtml='';var ra=(r.recent_activity||r.recent_actions||[]).slice(0,3);if(ra.length){raHtml='<div class="recent-actions">';for(var j=0;j<ra.length;j++){var a=ra[j];if(a.type==='llm'){raHtml+='<div class="ra-item"><span class="ra-tool" style="color:var(--accent)">\ud83e\udde0 LLM '+(a.elapsed_s||'?')+'s</span><span class="ra-args">'+esc(a.preview||'')+'</span><span class="ra-time">'+timeAgo(a.timestamp)+'</span></div>'}else{var em=actEmoji[a.tool]||'\u2699\ufe0f';raHtml+='<div class="ra-item"><span class="ra-tool">'+em+' '+esc(a.tool)+'</span><span class="ra-args">'+esc(a.preview||briefArgs(a.arguments)||'')+'</span><span class="ra-time">'+timeAgo(a.timestamp)+'</span></div>'}}raHtml+='</div>'}
    var hb2=r.last_heartbeat;var hbAge2=hb2?Math.floor((Date.now()-new Date(hb2))/1000):null;var hbCol2=hbAge2!==null?(hbAge2<60?'var(--green)':hbAge2<300?'var(--yellow)':'var(--red)'):'var(--text2)';var hbLabel=hb2?'<span style="font-size:10px;color:'+hbCol2+'"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:'+hbCol2+';margin-right:3px"></span>Active '+timeAgo(hb2)+'</span>':'';
    var wpc=r.weekly_profit;var wpcCol=wpc!=null?(wpc>=0?'var(--green)':'var(--red)'):'var(--text)';
    cards+='<div class="run-card'+(currentRun===r.run_id?' active':'')+'" onclick="selectRun(\''+r.run_id+'\')"><div class="label" style="display:flex;align-items:center;gap:8px">'+esc(r.label)+' '+hbLabel+'</div><div class="meta">'+esc(r.model||'')+' \u00b7 seed '+r.seed+' \u00b7 '+r.run_id+'</div><div class="stats"><div>Cash: <span class="val cash">'+fmtCash(r.cash)+'</span></div><div>Subs: <span class="val">'+fmt(r.subscribers)+'</span></div><div>Wk. Profit: <span class="val" style="color:'+wpcCol+'">'+(wpc!=null?fmtCash(wpc):'\u2014')+'</span></div><div>Divs: <span class="val divs">'+fmtCash(r.founder_dividends)+'</span></div></div><div class="progress-bar"><div class="progress-fill" style="width:'+p+'%"></div></div><div class="progress-text">Day '+displayVal(r.current_day,'?')+' / '+displayVal(r.total_days,'?')+' ('+p+'%) \u00b7 '+fmt(r.agent_turns||r.tool_calls_count)+' turns</div>'+raHtml+'</div>'}
  $('#runCards').innerHTML=cards;
  var sel=$('#runSelect');var cv=sel.value;sel.innerHTML='<option value="">— All Runs Overview —</option>';
  for(var i=0;i<runs.length;i++){var r=runs[i];sel.innerHTML+='<option value="'+r.run_id+'"'+(r.run_id===cv?' selected':'')+'>'+esc(r.label)+' ('+r.run_id+')</option>'}
}

function selectRun(id){currentRun=id||null;$('#runSelect').value=id;if(!id){$('#overviewSection').style.display='';$('#detailSection').classList.remove('visible');return}$('#overviewSection').style.display='none';$('#detailSection').classList.add('visible');renderDetail()}

function renderDetail(){
  if(!currentRun)return;var r=(allData.runs||[]).find(function(x){return x.run_id===currentRun});if(!r)return;
  var p=pct(r.current_day||0,r.total_days||1095);
  $('#detailTitle').textContent=r.label+' \u2014 '+r.model+' ('+r.run_id+')';
  var wpColor=r.weekly_profit!=null?(r.weekly_profit>=0?'color:var(--green)':'color:var(--red)'):'';
  var stats=[{l:'Day',v:displayVal(r.current_day,'?')+' / '+displayVal(r.total_days,'?')+' ('+p+'%)'},{l:'Cash',v:fmtCash(r.cash),c:(r.cash||0)<0?'color:var(--red)':'color:var(--green)'},{l:'Subscribers',v:fmt(r.subscribers)},{l:'Wk. Profit',v:r.weekly_profit!=null?fmtCash(r.weekly_profit):'\u2014',c:wpColor},{l:'F. Dividends',v:fmtCash(r.founder_dividends),c:'color:var(--yellow)'},{l:'Agent Turns',v:fmt(r.agent_turns||r.tool_calls_count)},{l:'Avg Day Time',v:r.timing_avg_day?r.timing_avg_day+'s':'\u2014',c:'color:var(--accent)'}];
  $('#detailStats').innerHTML=stats.map(function(s){return'<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 14px"><div style="font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:.5px">'+s.l+'</div><div style="font-size:18px;font-weight:700;margin-top:2px;'+(s.c||'')+'">'+s.v+'</div></div>'}).join('');
  renderDiscovery(r);renderCharts(r);renderNewCharts(r);renderActions(r);renderResponses(r);renderRationales(r);renderTiming(r);renderAgentPosts(r);renderCustomerPosts(r);
}

function renderDiscovery(r){
  var groups=(r.group_discovery||[]).filter(function(g){return g.info_level>=1});
  if(!groups.length){$('#discoverySection').innerHTML='';return}
  var h='<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:4px"><h3 style="font-size:13px;color:var(--text2);margin-bottom:10px">Discovered Customer Groups ('+groups.length+')</h3><table class="discovery-table"><thead><tr><th>Group</th><th>Info Level</th><th>Discovered Day</th></tr></thead><tbody>';
  for(var i=0;i<groups.length;i++){var g=groups[i];
    h+='<tr><td><strong style="color:'+(GROUP_COLORS[g.group_id]||'var(--text)')+'">'+esc(g.group_id)+'</strong></td><td>'+g.info_level+'</td><td>'+(g.discovered_day!=null?'Day '+g.discovered_day:'\u2014')+'</td></tr>'}
  h+='</tbody></table></div>';
  $('#discoverySection').innerHTML=h;
}

function renderCharts(r){
  if(charts.cash)charts.cash.destroy();
  var lineOpts={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{point:{radius:0},line:{borderWidth:2}}};
  charts.cash=new Chart($('#cashChart').getContext('2d'),{type:'line',options:lineOpts,data:{labels:(r.cash_series||[]).map(function(d){return d.day}),datasets:[{label:'Cash',data:(r.cash_series||[]).map(function(d){return d.cash}),borderColor:'#3fb950',backgroundColor:'rgba(63,185,80,0.1)',fill:true}]}});
  if(charts.subs)charts.subs.destroy();
  var seatData=r.seat_series||[];
  var subData=r.sub_series||[];
  var byGroup=r.seat_series_by_group||[];
  var stackOpts={responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:9},boxWidth:10}},tooltip:{mode:'index',intersect:false}},scales:{x:{stacked:true,grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{stacked:true,grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{point:{radius:0},line:{borderWidth:1}}};
  if(byGroup.length){
    var sgDays=[...new Set(byGroup.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var sgGroups=[...new Set(byGroup.map(function(d){return d.group_id}))].sort();
    var sgKey={};
    for(var i=0;i<byGroup.length;i++){var d=byGroup[i];sgKey[d.day+'|'+d.group_id]=d.count}
    var sgDatasets=sgGroups.map(function(gid){
      var c=GROUP_COLORS[gid]||'#888';
      return{label:gid,data:sgDays.map(function(day){return sgKey[day+'|'+gid]||0}),borderColor:c,backgroundColor:c+'55',fill:true}
    });
    charts.subs=new Chart($('#subsChart').getContext('2d'),{type:'line',options:stackOpts,data:{labels:sgDays,datasets:sgDatasets}})
  }
  else if(seatData.length){charts.subs=new Chart($('#subsChart').getContext('2d'),{type:'line',options:stackOpts,data:{labels:seatData.map(function(d){return d.day}),datasets:[{label:'Individual',data:seatData.map(function(d){return d.individual}),borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,0.3)',fill:true},{label:'Enterprise Seats',data:seatData.map(function(d){return d.enterprise_seats}),borderColor:'#f0883e',backgroundColor:'rgba(240,136,62,0.3)',fill:true}]}})}
  else{charts.subs=new Chart($('#subsChart').getContext('2d'),{type:'line',options:lineOpts,data:{labels:subData.map(function(d){return d.day}),datasets:[{label:'Subscribers',data:subData.map(function(d){return d.subscribers}),borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,0.1)',fill:true}]}})};
  if(charts.div)charts.div.destroy();
  charts.div=new Chart($('#divChart').getContext('2d'),{type:'line',options:lineOpts,data:{labels:(r.dividend_series||[]).map(function(d){return d.day}),datasets:[{label:'Founder Dividends',data:(r.dividend_series||[]).map(function(d){return d.dividends}),borderColor:'#d29922',backgroundColor:'rgba(210,153,34,0.1)',fill:true}]}});
  // Weekly Profit chart (bar chart: revenue, costs, profit line)
  if(charts.profit)charts.profit.destroy();
  var profitData=r.profit_series||[];
  if(profitData.length){
    var profitOpts={responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:10}}}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10},callback:function(v){return'$'+fmt(v)}}}},elements:{point:{radius:2},line:{borderWidth:2}}};
    charts.profit=new Chart($('#profitChart').getContext('2d'),{type:'bar',options:profitOpts,data:{labels:profitData.map(function(d){return'Day '+d.day}),datasets:[{type:'bar',label:'Revenue',data:profitData.map(function(d){return d.revenue}),backgroundColor:'rgba(63,185,80,0.6)',borderColor:'#3fb950',borderWidth:1},{type:'bar',label:'Costs',data:profitData.map(function(d){return Math.abs(d.costs)}),backgroundColor:'rgba(248,81,73,0.6)',borderColor:'#f85149',borderWidth:1},{type:'line',label:'Profit',data:profitData.map(function(d){return d.profit}),borderColor:'#58a6ff',backgroundColor:'transparent',borderWidth:2,pointRadius:3,pointBackgroundColor:profitData.map(function(d){return d.profit>=0?'#3fb950':'#f85149'})}]}})
  }
}

function renderNewCharts(r){
  // Reputation chart
  var repData=r.reputation_series||[];
  if(charts.reputation)charts.reputation.destroy();
  if(repData.length){
    var repGroups=[...new Set(repData.map(function(d){return d.group_id}))];
    var repDays=[...new Set(repData.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var repDatasets=repGroups.map(function(gid){
      var gdata=repData.filter(function(d){return d.group_id===gid});
      var byDay={};gdata.forEach(function(d){byDay[d.day]=d.reputation});
      return{label:gid,data:repDays.map(function(d){return byDay[d]||null}),borderColor:GROUP_COLORS[gid]||'#888',borderWidth:2,fill:false,pointRadius:0}});
    charts.reputation=new Chart($('#reputationChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:10}}}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{point:{radius:0},line:{borderWidth:2}}},data:{labels:repDays,datasets:repDatasets}});
  }

  // Q_min chart
  var qminData=r.qmin_series||[];
  if(charts.qmin)charts.qmin.destroy();
  if(qminData.length){
    var qGroups=[...new Set(qminData.map(function(d){return d.group_id}))];
    var qDays=[...new Set(qminData.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var qDatasets=qGroups.map(function(gid){
      var gdata=qminData.filter(function(d){return d.group_id===gid});
      var byDay={};gdata.forEach(function(d){byDay[d.day]=d.q_min});
      return{label:gid,data:qDays.map(function(d){return byDay[d]||null}),borderColor:GROUP_COLORS[gid]||'#888',borderWidth:2,fill:false,pointRadius:0}});
    charts.qmin=new Chart($('#qminChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:10}}}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{point:{radius:0},line:{borderWidth:2}}},data:{labels:qDays,datasets:qDatasets}});
  }

  // Per-group q_bias drift only chart (excludes global accumulator)
  var qminDriftOnlyData=r.qmin_drift_only_series||[];
  if(charts.qminDriftOnly)charts.qminDriftOnly.destroy();
  if(qminDriftOnlyData.length){
    var qdGroups=[...new Set(qminDriftOnlyData.map(function(d){return d.group_id}))];
    var qdDays=[...new Set(qminDriftOnlyData.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var qdDatasets=qdGroups.map(function(gid){
      var gdata=qminDriftOnlyData.filter(function(d){return d.group_id===gid});
      var byDay={};gdata.forEach(function(d){byDay[d.day]=d.drift_q_bias});
      return{label:gid,data:qdDays.map(function(d){return byDay[d]||null}),borderColor:GROUP_COLORS[gid]||'#888',borderWidth:1.5,fill:false,pointRadius:0}});
    charts.qminDriftOnly=new Chart($('#qminDriftOnlyChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:9}},position:'right'}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{point:{radius:0},line:{borderWidth:1.5}}},data:{labels:qdDays,datasets:qdDatasets}});
  }

  // Quality chart (group × plan)
  var qualData=r.quality_series||[];
  if(charts.quality)charts.quality.destroy();
  if(qualData.length){
    var qKeys=[...new Set(qualData.map(function(d){return d.group_id+'_'+d.plan}))];
    var qualDays=[...new Set(qualData.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var planDash={'A':[],'B':[5,5],'C':[2,2]};
    var qualDatasets=qKeys.map(function(key){
      var parts=key.split('_');var gid=parts[0];var plan=parts[1];
      var gdata=qualData.filter(function(d){return d.group_id===gid&&d.plan===plan});
      var byDay={};gdata.forEach(function(d){byDay[d.day]=d.quality});
      return{label:gid+' '+plan,data:qualDays.map(function(d){return byDay[d]||null}),borderColor:GROUP_COLORS[gid]||'#888',borderWidth:1.5,borderDash:planDash[plan]||[],fill:false,pointRadius:0}});
    charts.quality=new Chart($('#qualityChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:9}},position:'right'}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{point:{radius:0},line:{borderWidth:1.5}}},data:{labels:qualDays,datasets:qualDatasets}});
  }

  // Ads Revenue per group chart
  var adsData=r.ads_revenue_series||[];
  if(charts.adsRevenue)charts.adsRevenue.destroy();
  if(adsData.length){
    var adsGroups=[...new Set(adsData.map(function(d){return d.group_id}))];
    var adsDays=[...new Set(adsData.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var adsDatasets=adsGroups.map(function(gid){
      var gdata=adsData.filter(function(d){return d.group_id===gid});
      var byDay={};gdata.forEach(function(d){byDay[d.day]=d.revenue});
      return{label:gid,data:adsDays.map(function(d){return byDay[d]||null}),borderColor:GROUP_COLORS[gid]||'#888',borderWidth:1.5,fill:false,pointRadius:0}});
    charts.adsRevenue=new Chart($('#adsRevenueChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:9}},position:'right'}},scales:{x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10},callback:function(v){return '$'+v.toLocaleString()}}}},elements:{point:{radius:0},line:{borderWidth:1.5}}},data:{labels:adsDays,datasets:adsDatasets}});
  }

  // Weekly churn rate per group chart
  var churnData=r.weekly_churn_by_group_series||[];
  if(charts.churn)charts.churn.destroy();
  if(churnData.length){
    var churnGroups=[...new Set(churnData.map(function(d){return d.group_id}))];
    var churnDays=[...new Set(churnData.map(function(d){return d.day}))].sort(function(a,b){return a-b});
    var churnDatasets=churnGroups.map(function(gid){
      var gdata=churnData.filter(function(d){return d.group_id===gid});
      var byDay={};gdata.forEach(function(d){byDay[d.day]=d});
      return{label:gid,data:churnDays.map(function(d){var e=byDay[d];return e?e.churn_rate*100:null}),_meta:byDay,borderColor:GROUP_COLORS[gid]||'#888',borderWidth:1.5,fill:false,pointRadius:0,spanGaps:true}});
    charts.churn=new Chart($('#churnChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:9}},position:'right'},tooltip:{callbacks:{label:function(ctx){var ds=ctx.dataset;var d=ctx.label;var e=ds._meta?ds._meta[d]:null;if(!e)return ds.label+': —';return ds.label+': '+(e.churn_rate*100).toFixed(2)+'% ('+e.cancelled+' churned / '+e.active_at_start+' active; vol='+e.voluntary+', invol='+e.involuntary+')'}}}},scales:{x:{title:{display:true,text:'Day',color:'#8b949e',font:{size:10}},grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{title:{display:true,text:'Trailing 7-day churn rate',color:'#8b949e',font:{size:10}},grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10},callback:function(v){return v.toFixed(2)+'%'}}}},elements:{point:{radius:0},line:{borderWidth:1.5}}},data:{labels:churnDays,datasets:churnDatasets}});
  }

  // Timing chart (stacked bar)
  var ds=r.timing_day_summaries||[];
  if(charts.timing)charts.timing.destroy();
  if(ds.length>0){
    var step=Math.max(1,Math.floor(ds.length/100));
    var sampled=ds.filter(function(_,i){return i%step===0||i===ds.length-1});
    charts.timing=new Chart($('#timingChart').getContext('2d'),{type:'bar',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:10}}}},scales:{x:{stacked:true,grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:9}}},y:{stacked:true,grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}}},elements:{bar:{borderRadius:1}}},data:{labels:sampled.map(function(d){return d.day}),datasets:[{label:'LLM',data:sampled.map(function(d){return d.llm_total_s||0}),backgroundColor:'rgba(88,166,255,0.7)'},{label:'step_day',data:sampled.map(function(d){return d.step_day_s||0}),backgroundColor:'rgba(240,136,62,0.7)'},{label:'tools',data:sampled.map(function(d){return d.tool_total_s||0}),backgroundColor:'rgba(188,140,255,0.7)'}]}});
  }
  // Cash prediction % error (1wk / 4wk / 12wk / 26wk) on one plot — x-axis = target_day
  var predData=r.prediction_accuracy_series||[];
  if(charts.prediction)charts.prediction.destroy();
  if(predData.length){
    var horizonMeta={7:{label:'1wk (+7d)',color:'#58a6ff'},28:{label:'4wk (+28d)',color:'#d29922'},84:{label:'12wk (+84d)',color:'#bc8cff'},182:{label:'26wk (+182d)',color:'#f85149'}};
    var allDays=[...new Set(predData.map(function(d){return d.target_day}))].sort(function(a,b){return a-b});
    var predDatasets=[7,28,84,182].map(function(h){
      var rows=predData.filter(function(d){return d.horizon_days===h});
      var byDay={};rows.forEach(function(d){byDay[d.target_day]=d.pct_diff});
      return{label:horizonMeta[h].label,data:allDays.map(function(d){return d in byDay?byDay[d]:null}),borderColor:horizonMeta[h].color,backgroundColor:horizonMeta[h].color+'22',borderWidth:2,fill:false,pointRadius:2,spanGaps:true};
    });
    charts.prediction=new Chart($('#predictionChart').getContext('2d'),{type:'line',options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#8b949e',font:{size:10}}},tooltip:{callbacks:{title:function(items){return'Target day '+items[0].label},label:function(ctx){return ctx.dataset.label+': '+(ctx.parsed.y==null?'\u2014':ctx.parsed.y.toFixed(1)+'%')}}}},scales:{x:{title:{display:true,text:'Target day (day when actual cash was measured)',color:'#8b949e',font:{size:10}},grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},y:{title:{display:true,text:'% error: (predicted − actual) / |actual| × 100',color:'#8b949e',font:{size:10}},grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10},callback:function(v){return v.toFixed(0)+'%'}}}},elements:{line:{borderWidth:2}}},data:{labels:allDays,datasets:predDatasets}});
  }

  // Per-horizon cash forecast plots: predicted cash + 95% CI band + actual cash overlay
  var cashSeries=r.cash_series||[];
  var horizonsCash=[7,28,84,182];
  var horizonCanvasIds={7:'predCashH7',28:'predCashH28',84:'predCashH84',182:'predCashH182'};
  for(var hi=0;hi<horizonsCash.length;hi++){
    var hh=horizonsCash[hi];
    var key='predCash'+hh;
    if(charts[key])charts[key].destroy();
    var hRows=predData.filter(function(d){return d.horizon_days===hh&&d.predicted_lower!=null&&d.predicted_upper!=null});
    if(!hRows.length)continue;
    // Sort by target_day so the band fills correctly.
    hRows.sort(function(a,b){return a.target_day-b.target_day});
    var actualPts=cashSeries.map(function(d){return{x:d.day,y:d.cash}});
    var predictedPts=hRows.map(function(d){return{x:d.target_day,y:d.predicted_value}});
    var upperPts=hRows.map(function(d){return{x:d.target_day,y:d.predicted_upper}});
    var lowerPts=hRows.map(function(d){return{x:d.target_day,y:d.predicted_lower}});
    var ctx=$('#'+horizonCanvasIds[hh]);
    if(!ctx)continue;
    charts[key]=new Chart(ctx.getContext('2d'),{
      type:'line',
      data:{datasets:[
        {label:'Upper 95% CI',data:upperPts,borderColor:'rgba(188,140,255,0)',pointRadius:0,fill:false,order:4,parsing:false},
        {label:'95% CI band',data:lowerPts,borderColor:'rgba(188,140,255,0)',backgroundColor:'rgba(188,140,255,0.18)',pointRadius:0,fill:'-1',order:3,parsing:false},
        {label:'Predicted cash',data:predictedPts,borderColor:'#bc8cff',backgroundColor:'transparent',borderWidth:2,pointRadius:2.5,pointBackgroundColor:'#bc8cff',fill:false,order:1,parsing:false,spanGaps:true},
        {label:'Actual cash',data:actualPts,borderColor:'#3fb950',backgroundColor:'rgba(63,185,80,0.06)',borderWidth:2,pointRadius:0,fill:false,order:0,parsing:false}
      ]},
      options:{
        responsive:true,maintainAspectRatio:false,
        plugins:{
          legend:{labels:{color:'#8b949e',font:{size:10},filter:function(item){return item.text!=='Upper 95% CI'}}},
          tooltip:{mode:'nearest',intersect:false,callbacks:{label:function(c){return c.dataset.label+': $'+(c.parsed.y!=null?Math.round(c.parsed.y).toLocaleString():'?')}}}
        },
        scales:{
          x:{type:'linear',title:{display:true,text:'Day',color:'#8b949e',font:{size:10}},grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10}}},
          y:{title:{display:true,text:'Cash ($)',color:'#8b949e',font:{size:10}},grid:{color:'#21262d'},ticks:{color:'#8b949e',font:{size:10},callback:function(v){return'$'+(Math.abs(v)>=1000?(v/1000).toFixed(0)+'k':v.toFixed(0))}}}
        },
        elements:{line:{borderWidth:2}}
      }
    });
  }

  // Cumulative timing bars
  var tl=r.timing_total_llm||0,ts=r.timing_total_step||0,tt=r.timing_total_tool||0;
  var tot=tl+ts+tt;
  var barsEl=$('#timingBars');
  if(tot>0){
    var pl=tl/tot*100,ps=ts/tot*100,pt=tt/tot*100;
    function fmtDur(s){if(s<60)return s.toFixed(0)+'s';if(s<3600)return(s/60).toFixed(1)+'m';return(s/3600).toFixed(1)+'h'}
    barsEl.innerHTML='<div style="margin-bottom:12px"><div style="display:flex;height:24px;border-radius:4px;overflow:hidden"><div style="width:'+pl+'%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#000">LLM '+pl.toFixed(0)+'%</div><div style="width:'+ps+'%;background:#f0883e;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#000">step '+ps.toFixed(0)+'%</div><div style="width:'+pt+'%;background:var(--purple);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#000">tools '+pt.toFixed(0)+'%</div></div></div><div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px;font-size:13px"><div><span style="color:var(--text2)">LLM:</span> <strong style="color:var(--accent)">'+fmtDur(tl)+'</strong></div><div><span style="color:var(--text2)">step_day:</span> <strong style="color:#f0883e">'+fmtDur(ts)+'</strong></div><div><span style="color:var(--text2)">tools:</span> <strong style="color:var(--purple)">'+fmtDur(tt)+'</strong></div><div><span style="color:var(--text2)">avg/day:</span> <strong>'+(r.timing_avg_day||'\u2014')+'s</strong></div></div>';
  }else{barsEl.innerHTML='<div style="color:var(--text2);font-size:13px">No timing data yet</div>'}
}

function renderActions(r){
  var actions=r.recent_actions||[];if(!actions.length){$('#actionList').innerHTML='<div class="loading">No actions yet</div>';return}
  var emojis={'bash':'\ud83d\udd27','read_file':'\ud83d\udcc2','write_file':'\u270d\ufe0f','edit_file':'\u270f\ufe0f','search_files':'\ud83d\udd0d','glob_files':'\ud83d\udd0d','_dashboard':'\ud83d\udcca','_reasoning':'\ud83d\udcad'};
  var h='';for(var i=0;i<actions.length;i++){var a=actions[i];var em=emojis[a.tool]||'\u2699\ufe0f';var aid=_actionId(a,i);
    h+='<div class="action-item" data-action-id="'+aid+'"><div class="action-header"><span class="day-badge">Day '+a.day+'</span><span class="turn-badge">Turn '+a.turn+'</span><span class="tool-name">'+em+' '+esc(a.tool)+'</span><span class="timestamp">'+timeAgo(a.timestamp)+'</span></div>'+(fmtArgs(a.arguments)?'<div class="args">'+fmtArgs(a.arguments)+'</div>':'')+'<span class="result-toggle" onclick="_markInteraction();this.nextElementSibling.classList.toggle(\'visible\');this.textContent=this.textContent===\'\u25b6 Show result\'?\'\u25bc Hide result\':\'\u25b6 Show result\'">\u25b6 Show result</span><div class="result-content">'+fmtResult(a.result)+'</div></div>'}
  $('#actionList').innerHTML=h;
}

function renderResponses(r){
  var resps=r.recent_responses||[];if(!resps.length){$('#responseList').innerHTML='<div class="loading">No responses yet</div>';return}
  var h='';for(var i=0;i<resps.length;i++){var rr=resps[i];var raw=rr.raw_response||{};var u=raw.usage||{};
    var inTok=u.prompt_tokens||u.input_tokens||0;var outTok=u.completion_tokens||u.output_tokens||0;
    var ct='',tcs=[];
    if(raw.choices&&raw.choices.length){var msg=raw.choices[0].message||{};ct=msg.content||'';tcs=msg.tool_calls||[]}
    else if(raw.content&&Array.isArray(raw.content)){var texts=[],tools=[];raw.content.forEach(function(b){if(b.type==='text')texts.push(b.text);else if(b.type==='tool_use')tools.push({function:{name:b.name,arguments:JSON.stringify(b.input||{})}})});ct=texts.join('\n');tcs=tools}
    h+='<div class="response-card"><div class="resp-header"><span class="day-badge">Day '+rr.day+'</span><span class="turn-badge">Turn '+rr.turn+'</span><span style="font-size:12px;color:var(--text2)">'+(inTok?fmt(inTok)+' in / '+fmt(outTok)+' out':'')+'</span><span class="timestamp">'+timeAgo(rr.timestamp)+'</span></div>'+(ct?'<div class="content-block">'+fmtResult(ct)+'</div>':'')+(tcs.length?'<div style="margin-top:8px"><div style="font-size:12px;color:var(--text2)">Tool calls ('+tcs.length+'):</div>'+tcs.map(function(tc){var a='';try{a=JSON.stringify(JSON.parse(tc.function.arguments||'{}'),null,2)}catch(e){a=tc.function.arguments||''}return'<div class="tc-item"><span class="tc-name">'+esc(tc.function.name||'?')+'</span><pre>'+esc(a)+'</pre></div>'}).join('')+'</div>':'')+'</div>'}
  $('#responseList').innerHTML=h;
}

function renderAgentPosts(r){
  var posts=r.agent_social_posts||[];
  if(!posts.length){$('#agentPostList').innerHTML='<div class="loading">No agent posts yet</div>';return}
  var h='';
  // Overall next-day lead multiplier header
  var ndsm=r.next_day_social_multiplier||{};var ndKeys=Object.keys(ndsm).sort();
  if(ndKeys.length){h+='<div style="padding:10px 14px;margin-bottom:12px;background:var(--card);border:1px solid var(--border);border-radius:8px"><div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:6px">\ud83d\udcc8 Next-Day Lead Multiplier <span style="font-weight:400;color:var(--text2)">(from social media effects)</span></div><div>';
  for(var j=0;j<ndKeys.length;j++){var gid=ndKeys[j];var mv=ndsm[gid];var mc=mv>1.001?'var(--green)':mv<0.999?'var(--red)':'var(--text2)';h+='<span style="margin-right:10px;font-size:12px"><strong style="color:'+(GROUP_COLORS[gid]||'var(--text)')+'">'+esc(gid)+'</strong>: <span style="color:'+mc+';font-weight:600">'+mv.toFixed(4)+'x</span></span>'}
  h+='</div></div>'}
  for(var i=0;i<posts.length;i++){var p=posts[i];
    // Score badges per group
    var scores='';var effects=p.effect_by_group||{};
    var gids=Object.keys(effects).sort();
    for(var j=0;j<gids.length;j++){var gid=gids[j];scores+='<span style="margin-right:6px"><strong style="color:'+(GROUP_COLORS[gid]||'var(--text)')+'">'+esc(gid)+'</strong> '+scoreBadge(effects[gid])+'</span>'}
    // Views by group
    var vbg=p.views_by_group||{};var viewsStr='';
    var vgids=Object.keys(vbg).sort();
    for(var j=0;j<vgids.length;j++){viewsStr+='<span style="margin-right:8px;font-size:11px"><strong style="color:'+(GROUP_COLORS[vgids[j]]||'var(--text)')+'">'+vgids[j]+'</strong>: '+fmt(vbg[vgids[j]])+'</span>'}
    h+='<div class="social-post"><div class="post-meta"><span class="day-badge">Day '+p.day+'</span><span>\ud83d\udc41 '+fmt(p.views)+' views</span>'+(p.reply_to_post_id?'<span style="color:var(--accent)">\u21a9\ufe0f Reply to post #'+p.reply_to_post_id+'</span>':'')+'</div>';
    h+='<div class="post-content">'+esc(p.content)+'</div>';
    if(scores)h+='<div style="margin-top:6px;font-size:12px"><span style="color:var(--text2)">Scores:</span> '+scores+'</div>';
    if(viewsStr)h+='<div style="margin-top:2px;font-size:12px"><span style="color:var(--text2)">Views:</span> '+viewsStr+'</div>';
    // Judge reasoning per group
    var reasoning=p.reasoning_by_group||{};var rkeys=Object.keys(reasoning).sort();
    if(rkeys.length){h+='<details style="margin-top:6px"><summary style="font-size:12px;color:var(--accent);cursor:pointer">\u2696\ufe0f Judge Reasoning ('+rkeys.length+' groups)</summary>';
    for(var j=0;j<rkeys.length;j++){var rgid=rkeys[j];var rtxt=reasoning[rgid]||'';h+='<div style="margin:4px 0 4px 8px;padding:6px 10px;background:var(--bg);border-radius:6px;border-left:3px solid '+(GROUP_COLORS[rgid]||'var(--border)')+'"><div style="font-size:11px;margin-bottom:2px"><strong style="color:'+(GROUP_COLORS[rgid]||'var(--text)')+'">'+esc(rgid)+'</strong> '+scoreBadge((p.effect_by_group||{})[rgid]||0)+'</div><div style="font-size:12px;color:var(--text2);white-space:pre-wrap">'+esc(rtxt)+'</div></div>'}
    h+='</details>'}
    // Replies
    var replies=p.replies||[];
    for(var k=0;k<replies.length;k++){var rp=replies[k];
      h+='<div class="post-reply '+(rp.sentiment||'')+'"><div style="font-size:11px;color:var(--text2);margin-bottom:2px"><strong style="color:'+(GROUP_COLORS[rp.group_id]||'var(--text)')+'">'+esc(rp.group_id||'?')+'</strong> \u00b7 '+(rp.sentiment==='positive'?'\ud83d\udc4d':rp.sentiment==='negative'?'\ud83d\udc4e':'\ud83d\ude10')+' '+esc(rp.sentiment||'')+'</div><div>'+esc(rp.content)+'</div></div>'}
    h+='</div>'}
  $('#agentPostList').innerHTML=h;
}

function renderCustomerPosts(r){
  var posts=r.customer_social_posts||[];
  if(!posts.length){$('#customerPostList').innerHTML='<div class="loading">No customer posts yet</div>';return}
  var h='';
  for(var i=0;i<posts.length;i++){var p=posts[i];
    var sentColor=p.sentiment==='positive'?'var(--green)':p.sentiment==='negative'?'var(--red)':'var(--text2)';
    var sentEmoji=p.sentiment==='positive'?'\ud83d\udc4d':p.sentiment==='negative'?'\ud83d\udc4e':'\ud83d\ude10';
    h+='<div class="social-post"><div class="post-meta"><span class="day-badge">Day '+p.day+'</span><strong style="color:'+(GROUP_COLORS[p.group_id]||'var(--text)')+'">'+esc(p.group_id||'?')+'</strong><span style="color:'+sentColor+'">'+sentEmoji+' '+esc(p.sentiment||'')+'</span>'+(p.reply_to_agent_post_id?'<span style="color:var(--accent)">\u21a9\ufe0f Reply to agent post #'+p.reply_to_agent_post_id+'</span>':'')+'<span style="color:var(--text2)">cust #'+p.customer_id+'</span></div>';
    h+='<div class="post-content">'+esc(p.content)+'</div></div>'}
  $('#customerPostList').innerHTML=h;
}

function switchTab(t){$$('.tab').forEach(function(x){x.classList.remove('active')});$$('.tab-content').forEach(function(x){x.classList.remove('active')});var tabs=$$('.tab');var tabMap={'actions':0,'responses':1,'rationales':2,'timing':3,'agentPosts':4,'customerPosts':5};var idx=tabMap[t]||0;if(tabs[idx])tabs[idx].classList.add('active');var el=$('#'+t+'Tab');if(el)el.classList.add('active')}

function renderRationales(r){
  var rats=r.daily_rationales||[];if(!rats.length){$('#rationaleList').innerHTML='<div class="loading">No rationales yet</div>';return}
  var h='';for(var i=rats.length-1;i>=0;i--){var rt=rats[i];
    h+='<div class="action-item"><div class="action-header"><span class="day-badge">Day '+rt.day+'</span>'+(rt.turn?'<span class="turn-badge">Turn '+rt.turn+'</span>':'')+'<span style="color:var(--accent);font-weight:600">\ud83d\udcad Rationale</span><span class="timestamp">'+timeAgo(rt.timestamp)+'</span></div><div style="padding:8px 12px;white-space:pre-wrap;font-size:13px;color:var(--text);line-height:1.5;background:var(--bg);border-radius:6px;margin-top:6px">'+esc(rt.text)+'</div></div>'}
  $('#rationaleList').innerHTML=h;
}

function renderTiming(r){
  var turns=r.timing_recent_turns||[];
  if(!turns.length){$('#timingList').innerHTML='<div class="loading">No timing data yet</div>';return}
  var h='';for(var i=0;i<turns.length;i++){var t=turns[i];
    if(t.event==='llm_call'){h+='<div class="action-item"><div class="action-header"><span class="day-badge">Day '+t.day+'</span><span class="turn-badge">Turn '+t.turn+'</span><span style="color:var(--accent);font-weight:600">LLM '+t.elapsed_s+'s</span><span style="color:var(--purple);margin-left:8px">\u2192 '+esc(t.tool||'')+'</span><span style="color:var(--text2);font-size:11px;margin-left:auto">'+esc((t.tool_preview||'').substring(0,80))+'</span></div></div>'}
    else if(t.event==='tool_exec'){h+='<div class="action-item"><div class="action-header"><span class="day-badge">Day '+t.day+'</span><span class="turn-badge">Turn '+t.turn+'</span><span style="color:var(--purple);font-weight:600">Tool '+t.elapsed_s+'s</span><span style="color:var(--yellow);margin-left:8px">'+esc(t.tool||'')+'</span></div></div>'}
  }
  $('#timingList').innerHTML=h;
}

fetchData();setInterval(fetchData,15000);
</script>
</body>
</html>"""
