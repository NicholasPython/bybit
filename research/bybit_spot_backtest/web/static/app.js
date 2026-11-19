const PREFIX = document.body.dataset.prefix || '';
const api = url => PREFIX + url;
const PAIRS = JSON.parse(document.body.dataset.instruments);
const RESEARCH = JSON.parse(document.body.dataset.researchStrategies || '{}');
const $ = id => document.getElementById(id);
const number = value => new Intl.NumberFormat('zh-CN').format(value || 0);
const money = value => new Intl.NumberFormat('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}).format(value || 0);
const cst = ms => ms ? new Date(ms).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',year:'2-digit',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
const esc = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const localInput = ms => new Date(ms - new Date(ms).getTimezoneOffset()*60000).toISOString().slice(0,16);
let lastCandles = [];
let lastEquity = [];
let lastHoldEquity = [];
let lastSingleEquity = [];
let resultGeneration = 0;
let marketPairs = [];
let initialWindowSet = false;
let lastChartQuery = null;
const strategyNames = {smart_portfolio:'屯币宝 · 智能组合',sma_cross:'双均线交叉',rsi_reversion:'RSI 均值回归',turtle:'海龟 · 现货趋势'};
Object.assign(strategyNames,Object.fromEntries(Object.entries(RESEARCH).map(([key,item])=>[key,item.name])));
const orderReasons = {entry:'突破建仓',add:'顺势加仓',stop:'ATR 止损',channel_exit:'通道退出',signal_entry:'信号买入',signal_exit:'信号退出',rebalance:'币池调仓',grid_buy:'网格买入',grid_sell:'网格卖出',budget_buy:'预算分批买入'};
const quantityText = value => new Intl.NumberFormat('zh-CN',{maximumSignificantDigits:9}).format(value);

function tickClock(){ $('clock').textContent = new Date().toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) + ' CST'; }
tickClock(); setInterval(tickClock, 1000);

for(const pair of PAIRS){
  for(const id of ['chartInst','btInst']){ const opt=document.createElement('option'); opt.value=pair; opt.textContent=pair; $(id).appendChild(opt); }
}
for(const [index,pair] of PAIRS.entries()){
  const row=document.createElement('div');row.className='allocation-row';
  const weight=[50,30,20][index] || 10;
  row.innerHTML=`<label class="coin-choice"><input type="checkbox" data-coin="${pair}" ${index<3?'checked':''}> ${pair.slice(0,-4)}</label><label class="weight-input"><input type="number" data-weight="${pair}" min="0.01" max="99.99" step="0.0001" value="${weight}" aria-label="${pair} 目标权重" ${index<3?'':'disabled'}><span>%</span></label>`;
  $('allocationInputs').appendChild(row);
}
function allocations(){
  return [...$('allocationInputs').querySelectorAll('input[data-coin]:checked')].map(input=>({inst_id:input.dataset.coin,weight_pct:Number($('allocationInputs').querySelector(`input[data-weight="${input.dataset.coin}"]`).value)}));
}
function allocationBudget(selected=allocations(),capital=Number($('capital').value),fee=Number($('fee').value)){
  return selected.map(a=>({...a,notional:capital*a.weight_pct/100/(1+fee/10000)}));
}
function validateInitialBudget(data){
  const budget=allocationBudget(data.allocations,data.capital,data.fee_bps);
  const bad=budget.find(a=>a.notional+Math.max(1e-9,data.min_trade_usdt*1e-12)<data.min_trade_usdt);
  if(bad)throw new Error(`${bad.inst_id} 预计初始成交额 ${money(bad.notional)} USDT，低于单笔最低成交额 ${money(data.min_trade_usdt)} USDT。请降低门槛（默认 5 USDT），或提高初始资金/权重。`);
}
function portfolioWindow(){
  const selected=allocations();
  if(selected.length<2)throw new Error('请选择至少 2 个币种');
  const ranges=selected.map(a=>marketPairs.find(p=>p.inst_id===a.inst_id));
  if(ranges.some(p=>!p||p.first_ms===null||p.last_ms===null))throw new Error('所选币种的数据尚未就绪');
  const start=Math.max(...ranges.map(p=>p.first_ms)),end=Math.min(...ranges.map(p=>p.last_ms+60000));
  if(end-start<30*60000)throw new Error('所选币种尚无至少 30 分钟的共同数据区间');
  return {start,end};
}
function refreshPortfolioControls(){
  const selected=allocations(),total=selected.reduce((sum,a)=>sum+a.weight_pct,0);
  $('weightSummary').textContent=`已选 ${selected.length} 个币种 · 权重合计 ${Number.isFinite(total)?total.toFixed(4).replace(/0+$/,'').replace(/\.$/,''):'—'}% / 100%`;
  $('weightSummary').classList.toggle('warn',selected.length<2||Math.abs(total-100)>0.000001);
  const budget=allocationBudget(selected),minimum=Number($('minTrade').value);
  const valid=budget.every(a=>Number.isFinite(a.notional)&&a.notional>=0);
  const bad=budget.filter(a=>a.notional+Math.max(1e-9,minimum*1e-12)<minimum);
  $('allocationBudget').textContent=valid?`预计初始成交额（已预留买入手续费）：${budget.map(a=>a.inst_id.slice(0,-4)+' '+money(a.notional)+' USDT').join('；')}${bad.length?'。'+bad.map(a=>a.inst_id.slice(0,-4)).join('、')+' 低于单笔门槛，请先调整。':''}`:'请输入有效的资金与手续费参数';
  $('allocationBudget').classList.toggle('warn',!valid||bad.length>0);
  try{const range=portfolioWindow();$('portfolioRange').textContent=`共同数据范围 ${cst(range.start)} — ${cst(range.end)} CST；提交时逐分钟检查缺口。`;}
  catch(err){$('portfolioRange').textContent=err.message;}
}
let researchRenderedStrategy = null;
function isResearchMulti(){return Boolean(RESEARCH[$('strategy').value]?.multi);}
function researchUniverse(){return [...$('researchCoins').querySelectorAll('input:checked')].map(input=>input.value);}
function researchWindow(){
  const selected=researchUniverse();
  if(selected.length<2)throw new Error('币池请选择至少 2 个币种');
  const ranges=selected.map(a=>marketPairs.find(p=>p.inst_id===a));
  if(ranges.some(p=>!p||p.first_ms===null||p.last_ms===null))throw new Error('所选币池的数据尚未就绪');
  const start=Math.max(...ranges.map(p=>p.first_ms)),end=Math.min(...ranges.map(p=>p.last_ms+60000));
  if(end-start<30*60000)throw new Error('币池尚无共同历史区间');
  return {start,end};
}
function refreshResearchRange(){
  if(!isResearchMulti())return;
  try{const range=researchWindow();$('researchRange').textContent=`已选 ${researchUniverse().length} 币 · 共同范围 ${cst(range.start)} — ${cst(range.end)} CST`;}
  catch(err){$('researchRange').textContent=err.message;}
}
function updateResearchControls(){
  const strategy=$('strategy').value,item=RESEARCH[strategy];
  if(!item)return;
  if(researchRenderedStrategy!==strategy){
    researchRenderedStrategy=strategy;$('researchFields').replaceChildren();
    for(const [key,f] of Object.entries(item.fields)){
      const label=document.createElement('label');label.textContent=f.label;
      const input=document.createElement(f.type==='select'?'select':'input');input.dataset.param=key;
      if(f.type==='select'){
        for(const [value,name] of f.options){const option=document.createElement('option');option.value=String(value);option.textContent=name;input.appendChild(option);}
      }else{input.type='number';input.min=f.min;input.max=f.max;input.step=f.step;input.required=true;}
      input.value=String(f.default);label.appendChild(input);$('researchFields').appendChild(label);
    }
  }
  $('researchNote').textContent=item.note;
  $('researchUniverse').classList.toggle('hidden',!item.multi);
  for(const input of $('researchCoins').querySelectorAll('input'))input.disabled=!item.multi;
  $('researchSelectAll').disabled=!item.multi;
  refreshResearchRange();
}
function researchRequestParams(){
  const item=RESEARCH[$('strategy').value],out={};
  for(const input of $('researchFields').querySelectorAll('[data-param]')){
    const f=item.fields[input.dataset.param];
    out[input.dataset.param]=typeof f.default==='string'?input.value:Number(input.value);
  }
  if(item.multi){out.universe=researchUniverse();if(out.universe.length<2)throw new Error('币池请选择至少 2 个币种');}
  return out;
}
for(const pair of PAIRS){
  const label=document.createElement('label');label.className='coin-choice allocation-row';
  const input=document.createElement('input');input.type='checkbox';input.value=pair;
  input.checked=['BTC-USDT','ETH-USDT','SOL-USDT','XRP-USDT','DOGE-USDT'].includes(pair);
  label.appendChild(input);label.appendChild(document.createTextNode(pair.slice(0,-4)));$('researchCoins').appendChild(label);
}
function updateStrategyControls(){
  const strategy=$('strategy').value,portfolio=strategy==='smart_portfolio';
  for(const [id,show] of [['smaParams',strategy==='sma_cross'],['rsiParams',strategy==='rsi_reversion'],['turtleParams',strategy==='turtle'],['researchParams',Boolean(RESEARCH[strategy])],['portfolioParams',portfolio],['singleInstField',!portfolio&&!isResearchMulti()]]){
    $(id).classList.toggle('hidden',!show);
    for(const input of $(id).querySelectorAll('input,select,button'))input.disabled=!show;
  }
  updateResearchControls();
  if(portfolio){
    for(const input of $('allocationInputs').querySelectorAll('input[data-coin]')){
      $('allocationInputs').querySelector(`input[data-weight="${input.dataset.coin}"]`).disabled=!input.checked;
    }
    const ratio=$('rebalanceMode').value==='threshold';
    $('thresholdField').classList.toggle('hidden',!ratio);$('thresholdPct').disabled=!ratio;
    $('intervalField').classList.toggle('hidden',ratio);$('rebalanceInterval').disabled=ratio;
    refreshPortfolioControls();
  }
}
function turtleRequestParams(){
  const fields={signal_minutes:'turtleTimeframe',entry_period:'turtleEntry',exit_period:'turtleExit',atr_period:'turtleAtr',risk_pct:'turtleRisk',stop_n:'turtleStop',add_n:'turtleAdd',max_units:'turtleUnits',min_trade_usdt:'turtleMinTrade'};
  const data=Object.fromEntries(Object.entries(fields).map(([key,id])=>[key,Number($(id).value)]));
  if(!Object.values(data).every(Number.isFinite))throw new Error('请输入有效的海龟参数');
  if(data.exit_period>=data.entry_period)throw new Error('海龟退出周期须小于入场周期');
  return data;
}
function addCalendarYears(ms,years){
  const date=new Date(ms),year=date.getUTCFullYear()+years,month=date.getUTCMonth();
  const day=Math.min(date.getUTCDate(),new Date(Date.UTC(year,month+1,0)).getUTCDate());
  return Date.UTC(year,month,day,date.getUTCHours(),date.getUTCMinutes());
}
function subtractCalendarYears(ms,years){
  const date=new Date(ms),year=date.getUTCFullYear()-years,month=date.getUTCMonth();
  // Feb 29 becomes Mar 1 when the starting year is not leap, so the
  // resulting range never exceeds the forward calendar-year limit.
  return Date.UTC(year,month,date.getUTCDate(),date.getUTCHours(),date.getUTCMinutes());
}
function fillBacktestWindow(){
  let range;
  if($('strategy').value==='smart_portfolio'){range=portfolioWindow();}
  else if(isResearchMulti()){range=researchWindow();}
  else{
    const pair=marketPairs.find(p=>p.inst_id===$('btInst').value);
    if(!pair||pair.first_ms===null||pair.last_ms===null)throw new Error('所选币种的数据尚未就绪');
    range={start:pair.first_ms,end:pair.last_ms+60000};
  }
  const value=$('portfolioDays').value;
  const requested=value.endsWith('y')?subtractCalendarYears(range.end,Number(value.slice(0,-1))):range.end-Number(value)*86400000;
  const start=Math.max(range.start,requested);
  if(range.end-start<30*60000)throw new Error('所选币种尚无至少 30 分钟的共同数据区间');
  $('btEnd').value=localInput(range.end);
  $('btStart').value=localInput(start);
  $('backtestRangeHint').textContent=`已填入 ${cst(start)} — ${cst(range.end)} CST${start>requested?'；所选区间超出已有历史，已按共同可用数据范围填入':''}。提交时逐分钟检查缺口。`;
}
const thisMinute = Math.floor(Date.now()/60000)*60000;
$('btEnd').value=localInput(thisMinute);
$('btStart').value=localInput(thisMinute-24*60*60000);
$('chartEnd').value=localInput(thisMinute);
$('chartStart').value=localInput(thisMinute-24*60*60000);

async function jsonFetch(url, options={}){
  const response=await fetch(url,options);
  let data;
  try{data=await response.json();}catch{throw new Error(`服务器返回 ${response.status}`);}
  if(!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}

async function refreshStatus(){
  try{
    const data=await jsonFetch(api('/api/status'));
    marketPairs=data.pairs;refreshPortfolioControls();refreshResearchRange();
    if(!initialWindowSet){
      try{const range=portfolioWindow();$('btEnd').value=localInput(range.end);$('btStart').value=localInput(Math.max(range.start,range.end-86400000));initialWindowSet=true;}catch{}
    }
    const total=data.pairs.reduce((n,p)=>n+p.rows,0);
    const complete=data.pairs.filter(p=>p.backfill_complete).length;
    $('heroRows').textContent=number(total)+' 根';
    const stale=data.pairs.filter(p=>!p.fresh).length;
    $('heroNote').textContent=`${complete}/${data.pairs.length} 个交易对已回溯完毕 · ${stale ? stale+' 个数据滞后' : '最新数据正常'} · 最近更新 ${cst(Math.max(...data.pairs.map(p=>p.latest_sync_ms||0)))}`;
    $('refreshTime').textContent=`检查时间 ${cst(data.server_time_ms)} CST · 每 30 秒更新`;
    const grid=$('coverage'); grid.replaceChildren();
    for(const pair of data.pairs){
      const card=document.createElement('div'); card.className='pair-card';card.tabIndex=0;card.setAttribute('role','button');
      const coin=pair.inst_id.slice(0,-4);
      const foot=pair.last_error ? '采集错误：'+pair.last_error : !pair.fresh ? '最新数据滞后' : pair.backfill_complete ? '历史回溯已完成' : '历史回溯进行中';
      card.innerHTML=`<div class="pair-top"><div><span class="pair-name">${esc(coin)}</span><span class="pair-quote">/ USDT</span></div><span class="state-dot ${pair.fresh?'':'stale'}"></span></div><div class="pair-rows">${number(pair.rows)}</div><div class="pair-range">${cst(pair.first_ms)} — ${cst(pair.last_ms)} CST</div><div class="pair-foot ${pair.last_error || pair.missing_minutes || !pair.fresh?'warn':''}">${esc(foot)} · 缺口 ${number(pair.missing_minutes)}</div>`;
      const select=()=>{ $('chartInst').value=pair.inst_id;$('btInst').value=pair.inst_id;loadChart();document.querySelector('.canvas-wrap').scrollIntoView({behavior:'smooth',block:'center'}); };
      card.addEventListener('click',select);card.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}});
      grid.appendChild(card);
    }
  }catch(err){$('heroNote').textContent='状态加载失败：'+err.message;}
}

function canvasContext(canvas){
  const box=canvas.getBoundingClientRect();const dpr=Math.min(window.devicePixelRatio||1,2);
  canvas.width=Math.max(1,Math.floor(box.width*dpr));canvas.height=Math.max(1,Math.floor(box.height*dpr));
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);return {ctx,w:box.width,h:box.height};
}
function emptyCanvas(canvas,message){const {ctx,w,h}=canvasContext(canvas);ctx.fillStyle='#0d151d';ctx.fillRect(0,0,w,h);ctx.fillStyle='#7c929c';ctx.font='13px sans-serif';ctx.textAlign='center';ctx.fillText(message,w/2,h/2);}

function drawCandles(data){
  const canvas=$('priceChart');if(!data.length){emptyCanvas(canvas,'该时间范围暂无已采集数据');return;}
  const {ctx,w,h}=canvasContext(canvas);ctx.fillStyle='#0d151d';ctx.fillRect(0,0,w,h);
  const left=57,right=18,top=22,bottom=34,volH=54,plotW=w-left-right,priceH=h-top-bottom-volH;
  let min=Infinity,max=-Infinity,vmax=0;
  for(const row of data){min=Math.min(min,row[3]);max=Math.max(max,row[2]);vmax=Math.max(vmax,row[5]);}
  const pad=(max-min)*.08||max*.001||1;min-=pad;max+=pad;
  const y=price=>top+(max-price)/(max-min)*priceH;
  ctx.strokeStyle='#22333f';ctx.lineWidth=1;ctx.fillStyle='#8195a0';ctx.font='11px sans-serif';ctx.textAlign='right';
  for(let i=0;i<=4;i++){const yy=top+i*priceH/4;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right,yy);ctx.stroke();ctx.fillText((max-(max-min)*i/4).toPrecision(5),left-8,yy+4);}
  const dx=plotW/data.length;const bw=Math.max(1,Math.min(9,dx*.62));
  data.forEach((row,i)=>{const x=left+(i+.5)*dx;const up=row[4]>=row[1];ctx.strokeStyle=ctx.fillStyle=up?'#6ce2b4':'#ed8490';ctx.beginPath();ctx.moveTo(x,y(row[2]));ctx.lineTo(x,y(row[3]));ctx.stroke();const y1=y(Math.max(row[1],row[4])),y2=y(Math.min(row[1],row[4]));ctx.fillRect(x-bw/2,y1,bw,Math.max(1,y2-y1));const vh=vmax?row[5]/vmax*volH*.82:0;ctx.globalAlpha=.4;ctx.fillRect(x-bw/2,h-bottom-vh,bw,vh);ctx.globalAlpha=1;});
  ctx.textAlign='center';ctx.fillStyle='#8195a0';for(let i=0;i<5;i++){const idx=Math.min(data.length-1,Math.round(i*(data.length-1)/4));ctx.fillText(cst(data[idx][0]),left+(idx+.5)*dx,h-12);}
}

function chartQuery(){
  const custom=$('chartRange').value==='custom';
  const end=custom ? Math.floor(new Date($('chartEnd').value).getTime()/60000)*60000 : Math.floor(Date.now()/60000)*60000;
  const start=custom ? Math.floor(new Date($('chartStart').value).getTime()/60000)*60000 : end-Number($('chartRange').value)*86400000;
  if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)throw new Error('请选择有效的起止时间');
  return {inst:$('chartInst').value,step:$('chartStep').value,start:String(start),end:String(end)};
}

async function loadChart(){
  $('chartSummary').textContent='正在读取 K 线…';
  $('qualitySummary').textContent='正在检查区间数据质量…';
  try{
    const query=chartQuery();lastChartQuery=query;
    const params=new URLSearchParams(query);
    const [data,quality]=await Promise.all([jsonFetch(api('/api/candles?')+params),jsonFetch(api('/api/quality?')+params)]);
    lastCandles=data.candles;drawCandles(lastCandles);
    const gaps=quality.gaps.slice(0,3).map(g=>`${cst(g.start_ms)} 起 ${g.minutes} 分钟`).join('、');
    $('qualitySummary').textContent=`质量检查：${number(quality.present)}/${number(quality.expected)} 根 · 覆盖率 ${quality.coverage_pct}% · 缺失 ${number(quality.missing)} 分钟 · 异常 OHLC ${number(quality.invalid)} 根${gaps?' · 首批缺口：'+gaps:''}${quality.gaps_truncated?'（仅显示前 20 段）':''}`;
    $('qualitySummary').classList.toggle('warn',quality.missing>0||quality.invalid>0);
    if(!lastCandles.length){$('chartSummary').textContent='该范围暂无数据，历史回溯仍在进行。';return;}
    const first=lastCandles[0],last=lastCandles[lastCandles.length-1];const change=(last[4]/first[1]-1)*100;
    $('chartSummary').innerHTML=`<span><strong>${esc(data.inst_id)}</strong></span><span>最新收盘 <strong>${money(last[4])}</strong></span><span>区间变化 <strong class="${change>=0?'up':'down'}">${change>=0?'+':''}${change.toFixed(2)}%</strong></span><span>${lastCandles.length} 根 · ${cst(first[0])} — ${cst(last[0])} CST</span>`;
  }catch(err){lastCandles=[];emptyCanvas($('priceChart'),err.message);$('chartSummary').textContent=err.message;$('qualitySummary').textContent='';}
}

function drawEquity(data,benchmark=[],single=[]){
  const canvas=$('equityChart');if(!canvas)return;const {ctx,w,h}=canvasContext(canvas);ctx.fillStyle='#101d24';ctx.fillRect(0,0,w,h);if(data.length<2)return;
  const all=[...data,...benchmark,...single];let min=Math.min(...all.map(x=>x[1])),max=Math.max(...all.map(x=>x[1]));const pad=(max-min)*.1||1;min-=pad;max+=pad;
  const left=12,right=12,top=17,bottom=23;
  ctx.strokeStyle='#25413f';ctx.beginPath();ctx.moveTo(left,h-bottom);ctx.lineTo(w-right,h-bottom);ctx.stroke();
  const first=data[0][0],duration=data[data.length-1][0]-first||1;
  for(const [series,color] of [[benchmark,'#efc77a'],[single,'#a8a2ff'],[data,'#70e3c5']]){
    if(!series.length)continue;ctx.beginPath();series.forEach((row,i)=>{const x=left+(row[0]-first)/duration*(w-left-right),y=top+(max-row[1])/(max-min)*(h-top-bottom);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();
  }
  ctx.fillStyle='#8da6a9';ctx.font='10px sans-serif';ctx.textAlign='left';ctx.fillText(money(max),left+2,top+6);ctx.fillText(money(min),left+2,h-6);
}

function showResult(job){
  const area=$('results');
  const generation=++resultGeneration;lastSingleEquity=[];
  if(job.state==='queued'||job.state==='running'){
    lastEquity=[];lastHoldEquity=[];
    area.innerHTML='<div class="empty-state"><div class="empty-icon">◌</div><strong>回测运行中</strong><span>读取数据、执行策略并计算净值…长区间可能需要数分钟，也可稍后在历史记录中查看。</span></div>';return;
  }
  if(job.state==='error'){
    lastEquity=[];lastHoldEquity=[];
    area.innerHTML=`<div class="result-head"><h3>回测未完成</h3></div><p class="error-box">${esc(job.error||'未知错误')}</p>`;return;
  }
  const r=job.result;lastEquity=r.equity_curve;lastHoldEquity=r.buy_hold_curve||[];const tone=r.return_pct>=0?'up':'down';
  const portfolio=r.strategy==='smart_portfolio',turtle=r.strategy==='turtle',research=Boolean(RESEARCH[r.strategy]),multi=Boolean(r.is_multi);
  const metrics=[['策略收益',`${r.return_pct>=0?'+':''}${r.return_pct.toFixed(2)}%`,tone],['期末账户总净值（含现金）',money(r.final_equity)+' USDT',''],[portfolio?'同权重组合持有':multi?'币池等权持有':'策略币种持有',`${r.buy_hold_pct>=0?'+':''}${r.buy_hold_pct.toFixed(2)}%`,r.buy_hold_pct>=0?'up':'down'],['最大回撤',r.max_drawdown_pct.toFixed(2)+'%','']];
  if(portfolio){
    metrics.push(['超额收益 vs 组合持有',r.excess_return_pp.toFixed(2)+' 个百分点',r.excess_return_pp>=0?'up':'down'],['调仓次数',number(r.rebalances)+' 次',''],['成交订单',number(r.order_count)+' 笔',''],['期末现金',money(r.cash_usdt)+' USDT',''],['估计卖出成本',money(r.estimated_exit_cost)+' USDT','']);
  }else{metrics.push([research?'已实现卖出订单':'完整买卖轮次',number(r.completed_trades)+' 次',''],['成交订单',number(r.order_count)+' 笔',''],['胜率',r.win_rate_pct===null?'—':r.win_rate_pct.toFixed(1)+'%','']);}
  if(turtle){
    metrics.push(['突破建仓',number(r.entries)+' 次',''],['顺势加仓',number(r.additions)+' 次',''],['ATR 止损 / 通道退出',number(r.stop_exits)+' / '+number(r.channel_exits)+' 次',''],['期末现金',money(r.cash_usdt)+' USDT',''],['期末持仓单位',number(r.position_units)+' / '+r.turtle_params.max_units,''],['当前止损价',r.stop_price===null?'—':quantityText(r.stop_price)+' USDT',''],['累计成交额',money(r.turnover_usdt)+' USDT',''],['模拟滑点成本',money(r.slippage_cost_usdt)+' USDT','']);
  }
  if(research){
    metrics.push(['期末现金',money(r.cash_usdt)+' USDT',''],['累计成交额',money(r.turnover_usdt)+' USDT',''],['模拟滑点成本',money(r.slippage_cost_usdt)+' USDT',''],['已实现盈亏',money(r.realized_pnl_usdt)+' USDT',r.realized_pnl_usdt>=0?'up':'down'],['跳过小额订单',number(r.skipped_orders)+' 笔','']);
    if(multi)metrics.push(['调仓次数',number(r.rebalances)+' 次',''],['期末持币数量',number(r.holdings.filter(a=>a.final_qty>0).length)+' 个','']);
    else if(r.strategy==='spot_grid')metrics.push(['完成网格轮次',number(r.grid_rounds)+' 次',''],['固定网格下界 / 上界',quantityText(r.grid_levels[0])+' / '+quantityText(r.grid_levels.at(-1))+' USDT','']);
    else if(r.strategy==='budget_dca')metrics.push(['已执行买入份数',number(r.dca_buys)+' / '+r.strategy_params.tranches,'']);
    else metrics.push(['ATR 止损',number(r.stop_exits)+' 次','']);
  }
  if(Number.isFinite(r.exposure_pct))metrics.push(['持仓时间',r.exposure_pct.toFixed(1)+'%','']);
  if(Number.isFinite(r.fees_paid))metrics.push(['累计手续费',money(r.fees_paid)+' USDT','']);
  if(Number.isFinite(r.avg_realized_trade_pct))metrics.push(['平均已平仓收益',r.avg_realized_trade_pct.toFixed(2)+'%','']);
  const fingerprint=r.data_sha256?` · 数据指纹 ${esc(r.data_sha256.slice(0,16))}`:'';
  const turtleNote=turtle?` · 信号 ${r.turtle_params.signal_minutes===1440?'日线（UTC）':r.turtle_params.signal_minutes/60+' 小时'}，入场/退出 ${r.turtle_params.entry_period}/${r.turtle_params.exit_period} 根，ATR ${r.turtle_params.atr_period} 根 · 完整信号 K 线 ${number(r.signal_bars)} 根（含区间内预热） · 资金不足缩小 ${number(r.cash_limited_orders)} 单，跳过 ${number(r.skipped_entries)} 次小额建仓 · 每分钟已有止损优先，最多加仓一次 · 滑点成本为实际模拟成交相对参考价的差额，期末估计退出成本未计入累计费用`:'';
  const researchNote=research?` · ${r.strategy==='spot_grid'?'网格每分钟检查':r.strategy==='budget_dca'?`每 ${number(r.strategy_params.dca_minutes)} 分钟按预算买入`:`信号 ${r.strategy_params.signal_minutes} 分钟，完整信号 K 线 ${number(r.signal_bars)} 根，预热 ${r.warmup_bars} 根`} · 卖出订单按投入成本计算已实现收益 · 累计费用不包含期末估计退出成本${multi?' · 固定币池有事后选币偏差':r.strategy==='spot_grid'?' · 每档固定预算，同分钟新买档位不可卖出':r.strategy==='budget_dca'?' · 全部预算在开始存入，余款保留现金':''}`:'';
  const researchHoldings=multi?`<h4>币池持仓与最近目标权重</h4><div class="holdings-wrap"><table class="holdings-table"><thead><tr><th>币种</th><th>目标 / 实际权重</th><th>期末数量</th></tr></thead><tbody>${r.holdings.map(a=>`<tr><td>${esc(a.inst_id)}</td><td>${a.target_weight_pct.toFixed(2)}% / ${a.actual_weight_pct.toFixed(2)}%</td><td>${quantityText(a.final_qty)}</td></tr>`).join('')}</tbody></table></div>`:'';
  const holdings=portfolio?`<h4>各币持仓 · 对比初始买入持有数量</h4><div class="holdings-wrap"><table class="holdings-table"><thead><tr><th>币种</th><th>目标 / 期末权重</th><th>初始 / 期末数量</th><th>数量变化</th></tr></thead><tbody>${r.holdings.map(a=>`<tr><td>${esc(a.inst_id.slice(0,-4))}</td><td>${a.weight_pct.toFixed(2)}% / ${a.actual_weight_pct.toFixed(2)}%</td><td>${quantityText(a.initial_qty)}<br>${quantityText(a.final_qty)}</td><td class="${a.qty_change_pct>=0?'up':'down'}">${a.qty_change_pct>=0?'+':''}${a.qty_change_pct.toFixed(2)}%</td></tr>`).join('')}</tbody></table></div>`:'';
  const modeNote=portfolio?` · ${r.rebalance_mode==='threshold'?'比例':'定时'}平衡，每 ${number(r.interval_minutes)} 分钟检查，阈值 ${r.threshold_pct} 个百分点 · 最小模拟成交额 ${money(r.min_trade_usdt)} USDT · 跳过 ${number(r.skipped_rebalances)} 次小额调仓 · 期末仍持币，净值扣除估计卖出成本`:r.open_position?' · 期末仍持仓，按末根收盘价扣除估计卖出成本':' · 期末空仓';
  const eventRows=portfolio?`<h4>最近调仓</h4><div class="order-list">${r.recent_rebalances.map(x=>`<div><span>${cst(x.ts_ms)} CST · ${number(x.orders)} 笔</span><span>触发偏离 ${x.max_deviation_pp.toFixed(3)} 个百分点</span></div>`).join('')||'区间内未触发有效调仓'}</div>`:'';
  area.innerHTML=`<div class="result-head"><h3>${esc(strategyNames[r.strategy]||'回测')}结果</h3><small>${portfolio||multi?r.holdings.length+' 个币种 · 每币 ':esc(r.inst_id)+' · '}${number(r.bars)} 根 K 线</small></div><div class="metric-grid">${metrics.map(m=>`<div class="metric"><span>${esc(m[0])}</span><strong class="${m[2]}">${esc(m[1])}</strong></div>`).join('')}</div><div class="single-comparison"><label>单币现货持有对照（可选）<select id="benchmarkCoin"><option value="">不比较</option>${PAIRS.map(inst=>`<option value="${inst}">${esc(inst.slice(0,-4))} / USDT</option>`).join('')}</select></label><p id="benchmarkNote" class="result-note" aria-live="polite">用全部初始资金买入所选币种，保持原回测区间、手续费及滑点；无需重跑策略。</p><div id="benchmarkMetrics" class="metric-grid hidden"></div><button type="button" id="downloadComparison" class="text-button hidden">下载含单币对照的 JSON</button></div><div class="curve-legend"><span>● 策略净值</span>${portfolio||research?`<span class="portfolio-hold">● ${multi?'币池等权持有':portfolio?'同权重组合持有':'策略币种持有'}</span>`:''}<span id="singleLegend" class="single-hold hidden"></span></div><div class="equity-wrap"><canvas id="equityChart" aria-label="策略与买入持有净值曲线"></canvas></div><p class="result-note">${cst(r.start_ms)} — ${cst(r.end_ms)} CST${modeNote}${turtleNote}${researchNote}${fingerprint}${r.last_signal_unfilled?' · 最后一分钟触发的信号未成交':''}</p>${holdings}${researchHoldings}${eventRows}<h4>最近成交${r.orders_truncated?`（仅保留最后 ${portfolio||research?50:30} 条）`:''}</h4><div class="order-list">${r.recent_orders.map(x=>`<div><span><b>${x.side==='buy'?'买入':'卖出'}</b> ${esc(x.inst_id||'')}${x.reason?' · '+esc(orderReasons[x.reason]||x.reason):''} · ${cst(x.ts_ms)} CST</span><span>${quantityText(x.qty)} × ${quantityText(x.price)}${x.pnl===undefined?'':` · 盈亏 ${money(x.pnl)}`}</span></div>`).join('')||'无成交'}</div><p class="result-note"><a href="${api('/api/backtests')}/${esc(job.id)}/download">下载 JSON 结果与参数</a>${portfolio?' · 所有统计覆盖完整区间，成交和调仓明细各保留最近 50 条':''}</p>`;
  let comparison=null;
  $('benchmarkCoin').addEventListener('change',async()=>{
    const select=$('benchmarkCoin'),inst=select.value;
    comparison=null;lastSingleEquity=[];
    $('benchmarkMetrics').replaceChildren();$('benchmarkMetrics').classList.add('hidden');
    $('downloadComparison').classList.add('hidden');$('singleLegend').classList.add('hidden');
    drawEquity(lastEquity,lastHoldEquity,lastSingleEquity);
    const note=$('benchmarkNote');note.classList.remove('warn');
    if(!inst){note.textContent='用全部初始资金买入所选币种，保持原回测区间、手续费及滑点；无需重跑策略。';return;}
    select.disabled=true;note.textContent=`正在读取 ${inst} 完整区间数据并计算持有收益…`;
    try{
      const data=await jsonFetch(`${api('/api/backtests')}/${job.id}/benchmark?`+new URLSearchParams({inst}));
      if(generation!==resultGeneration)return;
      comparison=data;const b=data.benchmark;lastSingleEquity=b.equity_curve;
      const metrics=[['单币持有收益',`${b.return_pct>=0?'+':''}${b.return_pct.toFixed(2)}%`,b.return_pct],['单币期末净值',money(b.final_equity)+' USDT',null],['单币最大回撤',b.max_drawdown_pct.toFixed(2)+'%',null],['策略超额 vs 单币',data.excess_return_pp.toFixed(2)+' 个百分点',data.excess_return_pp],['策略净值差 vs 单币',money(data.equity_difference_usdt)+' USDT',data.equity_difference_usdt]];
      $('benchmarkMetrics').innerHTML=metrics.map(m=>`<div class="metric"><span>${esc(m[0])}</span><strong class="${m[2]===null?'':m[2]>=0?'up':'down'}">${esc(m[1])}</strong></div>`).join('');
      $('benchmarkMetrics').classList.remove('hidden');$('downloadComparison').classList.remove('hidden');
      $('singleLegend').textContent=`● ${inst.slice(0,-4)} 单币持有`;$('singleLegend').classList.remove('hidden');
      note.textContent=`${inst} · 初始资金 ${money(b.capital)} USDT · 手续费 ${b.fee_bps} bps / 滑点 ${b.slippage_bps} bps · ${number(b.bars)} 根 1 分钟 K 线 · 首根开盘买入，期末净值扣除估计卖出成本，未实际卖出 · 数据指纹 ${b.data_sha256.slice(0,16)}${b.assumptions.exit_cost_method==='legacy_additive'?' · 沿用该历史回测的卖出成本近似口径':''}`;
      drawEquity(lastEquity,lastHoldEquity,lastSingleEquity);
    }catch(err){if(generation===resultGeneration){note.textContent=err.message;note.classList.add('warn');}}
    finally{if(generation===resultGeneration)select.disabled=false;}
  });
  $('downloadComparison').addEventListener('click',()=>{
    if(!comparison)return;
    const blob=new Blob([JSON.stringify({backtest:job,comparison},null,2)+'\n'],{type:'application/json'});
    const url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download=`backtest-${job.id}-${comparison.benchmark.inst_id}.json`;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  requestAnimationFrame(()=>{if(generation===resultGeneration)drawEquity(lastEquity,lastHoldEquity,lastSingleEquity);});
}

async function pollJob(id){
  const deadline=Date.now()+630000;
  while(Date.now()<deadline){
    await new Promise(resolve=>setTimeout(resolve,1500));
    try{const job=await jsonFetch(api('/api/backtests/')+id);showResult(job);if(job.state==='done'||job.state==='error'){await loadHistory();return;}}catch(err){$('results').innerHTML=`<p class="error-box">${esc(err.message)}</p>`;return;}
  }
  $('results').innerHTML='<p class="error-box">页面等待已结束，请在回测历史中查看任务状态。</p>';await loadHistory();
}

async function loadHistory(){
  const area=$('backtestHistory');
  try{
    const data=await jsonFetch(api('/api/backtests'));
    if(!data.jobs.length){area.textContent='尚无保存的回测。';return;}
    area.innerHTML=data.jobs.map(job=>`<div class="history-row"><div><strong>${esc(job.params.allocations?job.params.allocations.map(a=>a.inst_id.slice(0,-4)+':'+a.weight_pct+'%').join(' / '):job.params.universe?job.params.universe.map(a=>a.slice(0,-4)).join(' / '):job.params.inst_id)} · ${esc(strategyNames[job.params.strategy]||job.params.strategy)}</strong><span>${cst(job.created_at_ms)} CST · ${esc(job.state==='done'?'已完成':job.state==='error'?'失败':'运行中')}</span></div><div class="history-actions"><button type="button" data-action="open" data-id="${esc(job.id)}">查看</button><a href="${api('/api/backtests')}/${esc(job.id)}/download">下载</a><button type="button" data-action="delete" data-id="${esc(job.id)}" ${job.state==='running'||job.state==='queued'?'disabled':''}>删除</button></div></div>`).join('');
  }catch(err){area.textContent='历史记录加载失败：'+err.message;}
}

$('backtestHistory').addEventListener('click',async event=>{
  const button=event.target.closest('button[data-action]');if(!button)return;
  const id=button.dataset.id;if(!/^[0-9a-f]{24}$/.test(id))return;
  try{
    if(button.dataset.action==='open'){
      showResult(await jsonFetch(api('/api/backtests/')+id));$('results').scrollIntoView({behavior:'smooth',block:'center'});
    }else if(button.dataset.action==='delete'){
      if(button.dataset.confirm!=='yes'){button.dataset.confirm='yes';button.textContent='确认删除';return;}
      await jsonFetch(api('/api/backtests/')+id,{method:'DELETE',headers:{'X-CSRF-Token':document.body.dataset.csrf}});
      await loadHistory();
    }
  }catch(err){$('backtestHistory').textContent='操作失败：'+err.message;}
});

$('strategy').addEventListener('change',()=>{
  updateStrategyControls();
  if(($('strategy').value==='turtle'||RESEARCH[$('strategy').value])&&$('portfolioDays').value==='30'){
    $('portfolioDays').value='1y';
    if(marketPairs.length){try{fillBacktestWindow();}catch(err){$('backtestRangeHint').textContent=err.message;}}
  }
});
$('researchCoins').addEventListener('change',refreshResearchRange);
$('researchSelectAll').addEventListener('click',()=>{for(const input of $('researchCoins').querySelectorAll('input'))input.checked=true;refreshResearchRange();});
$('rebalanceMode').addEventListener('change',updateStrategyControls);
$('allocationInputs').addEventListener('change',updateStrategyControls);
$('allocationInputs').addEventListener('input',refreshPortfolioControls);
for(const id of ['capital','fee','minTrade'])$(id).addEventListener('input',refreshPortfolioControls);
$('resetMinTrade').addEventListener('click',()=>{$('minTrade').value='5';refreshPortfolioControls();});
$('equalWeights').addEventListener('click',()=>{
  const selected=allocations();if(!selected.length)return;const base=Math.floor(10000/selected.length),remainder=10000-base*selected.length;
  selected.forEach((a,i)=>{$('allocationInputs').querySelector(`input[data-weight="${a.inst_id}"]`).value=((base+(i<remainder?1:0))/100).toFixed(2);});refreshPortfolioControls();
});
$('usePortfolioRange').addEventListener('click',()=>{try{fillBacktestWindow();}catch(err){$('backtestRangeHint').textContent=err.message;}});
updateStrategyControls();
$('chartRange').addEventListener('change',()=>{
  const value=$('chartRange').value;$('customChartRange').classList.toggle('hidden',value!=='custom');
  if(value!=='custom'){
    const steps={'1m':60000,'5m':300000,'15m':900000,'1H':3600000,'4H':14400000,'1D':86400000};
    const duration=Number(value)*86400000;
    if(duration/steps[$('chartStep').value]>2000){
      $('chartStep').value=Object.keys(steps).find(step=>duration/steps[step]<=2000);
    }
  }
});
$('chartLoad').addEventListener('click',loadChart);
$('chartExport').addEventListener('click',()=>{
  try{
    const query=chartQuery();if(Number(query.end)-Number(query.start)>7*86400000)throw new Error('CSV 导出最多 7 天，请使用自定义范围');
    window.location.href=api('/api/candles.csv?')+new URLSearchParams(query);
  }catch(err){$('qualitySummary').textContent=err.message;}
});
$('syncNow').addEventListener('click',async()=>{
  const button=$('syncNow');button.disabled=true;$('ingestMessage').textContent='正在同步最新 K 线…';
  try{const result=await jsonFetch(api('/api/ingest/sync'),{method:'POST',headers:{'X-CSRF-Token':document.body.dataset.csrf}});$('ingestMessage').textContent=result.message;await refreshStatus();}
  catch(err){$('ingestMessage').textContent='同步未完成：'+err.message;}
  finally{button.disabled=false;}
});
$('backtestForm').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('runButton');button.disabled=true;
  try{
    const start=Math.floor(new Date($('btStart').value).getTime()/60000)*60000;
    const end=Math.floor(new Date($('btEnd').value).getTime()/60000)*60000;
    if(!Number.isFinite(start)||!Number.isFinite(end)||end-start<30*60000)throw new Error('回测区间须至少 30 分钟');
    if(end>addCalendarYears(start,5))throw new Error('时间范围最多 5 年（按日历年计算）');
    const data={inst_id:$('btInst').value,strategy:$('strategy').value,start,end,capital:Number($('capital').value),fee_bps:Number($('fee').value),slippage_bps:Number($('slippage').value),fast:Number($('fast').value),slow:Number($('slow').value),period:Number($('period').value),buy_below:Number($('buyBelow').value),sell_above:Number($('sellAbove').value)};
    if(data.strategy==='smart_portfolio'){
      data.allocations=allocations();data.rebalance_mode=$('rebalanceMode').value;data.threshold_pct=Number($('thresholdPct').value);data.interval_minutes=Number($('rebalanceInterval').value);data.min_trade_usdt=Number($('minTrade').value);
      if(data.allocations.length<2||Math.abs(data.allocations.reduce((sum,a)=>sum+a.weight_pct,0)-100)>0.000001)throw new Error('请选择至少 2 个币种，并将权重合计设为 100%');
      validateInitialBudget(data);
    }
    if(data.strategy==='turtle')Object.assign(data,turtleRequestParams());
    if(RESEARCH[data.strategy])Object.assign(data,researchRequestParams());
    const job=await jsonFetch(api('/api/backtests'),{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.body.dataset.csrf},body:JSON.stringify(data)});
    showResult({state:'queued'});await loadHistory();await pollJob(job.id);
  }catch(err){$('results').innerHTML=`<p class="error-box">${esc(err.message)}</p>`;}
  finally{button.disabled=false;}
});
window.addEventListener('resize',()=>{drawCandles(lastCandles);if(lastEquity.length)drawEquity(lastEquity,lastHoldEquity,lastSingleEquity);});
refreshStatus();loadChart();loadHistory();setInterval(refreshStatus,30000);
