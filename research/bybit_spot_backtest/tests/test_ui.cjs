const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');
const functions = source.slice(source.indexOf('function addCalendarYears('), source.indexOf('const thisMinute ='));
const timestamp = value => Date.parse(value + 'Z');

function controls(strategy, choice, pair) {
  const nodes = Object.fromEntries(['strategy', 'portfolioDays', 'btInst', 'btStart', 'btEnd', 'backtestRangeHint'].map(id => [id, {value: '', textContent: ''}]));
  nodes.strategy.value = strategy;
  nodes.portfolioDays.value = choice;
  nodes.btInst.value = 'BTCUSDT';
  const sandbox = {Date, $: id => nodes[id], marketPairs: [pair],
    isResearchMulti: () => false, portfolioWindow: () => ({start: pair.first_ms, end: pair.last_ms + 60000}),
    localInput: ms => new Date(ms).toISOString().slice(0, 16), cst: ms => new Date(ms).toISOString()};
  vm.createContext(sandbox);
  vm.runInContext(functions, sandbox);
  return {nodes, sandbox};
}

test('calendar shortcuts preserve time and respect leap-year upper bounds', () => {
  const pair = {inst_id: 'BTCUSDT', first_ms: timestamp('2018-01-01T00:00'), last_ms: timestamp('2024-02-29T12:29')};
  const {sandbox, nodes} = controls('sma_cross', '5y', pair);
  assert.equal(sandbox.subtractCalendarYears(timestamp('2026-10-06T12:30'), 5), timestamp('2021-10-06T12:30'));
  assert.equal(sandbox.addCalendarYears(timestamp('2020-02-29T12:30'), 5), timestamp('2025-02-28T12:30'));
  assert.equal(sandbox.addCalendarYears(timestamp('2019-03-01T12:30'), 5), timestamp('2024-03-01T12:30'));
  sandbox.fillBacktestWindow();
  assert.equal(nodes.btStart.value, '2019-03-01T12:30');
  assert.equal(nodes.btEnd.value, '2024-02-29T12:30');
});

test('five-year shortcut reports a shorter available shared history', () => {
  const pair = {inst_id: 'BTCUSDT', first_ms: timestamp('2023-10-07T00:00'), last_ms: timestamp('2026-10-06T23:59')};
  const {sandbox, nodes} = controls('smart_portfolio', '5y', pair);
  sandbox.fillBacktestWindow();
  assert.equal(nodes.btStart.value, '2023-10-07T00:00');
  assert.match(nodes.backtestRangeHint.textContent, /超出已有历史/);
});

test('year and day shortcuts work for single-market strategies', () => {
  const pair = {inst_id: 'BTCUSDT', first_ms: timestamp('2023-10-07T00:00'), last_ms: timestamp('2026-10-06T23:59')};
  const {sandbox, nodes} = controls('rsi_reversion', '1y', pair);
  sandbox.fillBacktestWindow();
  assert.equal(nodes.btStart.value, '2025-10-07T00:00');
  nodes.portfolioDays.value = '90';
  sandbox.fillBacktestWindow();
  assert.equal(Date.parse(nodes.btEnd.value + 'Z') - Date.parse(nodes.btStart.value + 'Z'), 90 * 86400000);
});

test('Turtle submits separate signal, sizing and execution parameters', () => {
  const values={turtleTimeframe:'1440',turtleEntry:'55',turtleExit:'20',turtleAtr:'20',turtleRisk:'1',turtleStop:'2',turtleAdd:'0.5',turtleUnits:'4',turtleMinTrade:'5'};
  const sandbox={$:id=>({value:values[id]})};
  vm.createContext(sandbox);
  const fn=source.slice(source.indexOf('function turtleRequestParams('),source.indexOf('function addCalendarYears('));
  vm.runInContext(fn,sandbox);
  const request=sandbox.turtleRequestParams();
  assert.equal(request.signal_minutes,1440);
  assert.equal(request.entry_period,55);
  assert.equal(request.exit_period,20);
  assert.equal(request.risk_pct,1);
  assert.equal(request.add_n,0.5);
  assert.equal(request.max_units,4);
  values.turtleExit='55';
  assert.throws(()=>sandbox.turtleRequestParams(),/退出周期/);
});

test('Turtle selection enables its parameters and disables other strategy inputs', () => {
  const ids=['strategy','smaParams','rsiParams','turtleParams','researchParams','portfolioParams','singleInstField'];
  const nodes=Object.fromEntries(ids.map(id=>[id,{value:'turtle',hidden:null,input:{disabled:null},classList:{toggle(_class,hidden){nodes[id].hidden=hidden;}},querySelectorAll(){return [this.input];}}]));
  const sandbox={$:id=>nodes[id],RESEARCH:{},isResearchMulti:()=>false,updateResearchControls:()=>{}};
  vm.createContext(sandbox);
  const fn=source.slice(source.indexOf('function updateStrategyControls('),source.indexOf('function turtleRequestParams('));
  vm.runInContext(fn,sandbox);
  sandbox.updateStrategyControls();
  for(const id of ['turtleParams','singleInstField']){
    assert.equal(nodes[id].hidden,false);
    assert.equal(nodes[id].input.disabled,false);
  }
  for(const id of ['smaParams','rsiParams','portfolioParams']){
    assert.equal(nodes[id].hidden,true);
    assert.equal(nodes[id].input.disabled,true);
  }
});

const researchCatalog=JSON.parse(execFileSync(process.env.BYBIT_TEST_PYTHON||'python3',['-c',"import sys,json;sys.path.insert(0,'web');from strategy_catalog import STRATEGIES;print(json.dumps(STRATEGIES))"],{cwd:path.join(__dirname,'..'),encoding:'utf8'}));

test('all nine strategies submit the shared schema and selected universe', () => {
  const fn=source.slice(source.indexOf('function researchRequestParams('),source.indexOf('for(const pair of PAIRS)',source.indexOf('function researchRequestParams(')));
  for(const [strategy,item] of Object.entries(researchCatalog)){
    const fields=Object.entries(item.fields).map(([key,f])=>({dataset:{param:key},value:String(f.default)}));
    const sandbox={RESEARCH:researchCatalog,$:id=>id==='strategy'?{value:strategy}:{querySelectorAll:()=>fields},researchUniverse:()=>['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT']};
    vm.createContext(sandbox);vm.runInContext(fn,sandbox);
    const request=sandbox.researchRequestParams();
    for(const [key,f] of Object.entries(item.fields))assert.equal(request[key],f.default,strategy+':'+key);
    if(item.multi)assert.equal(request.universe.length,5);
    else assert.equal(request.universe,undefined);
  }
});

test('all nine result variants render metrics, costs and benchmark controls', () => {
  const fn=source.slice(source.indexOf('function showResult('),source.indexOf('async function pollJob('));
  for(const [strategy,item] of Object.entries(researchCatalog)){
    const nodes={};
    const sandbox={RESEARCH:researchCatalog,PAIRS:['BTCUSDT','ETHUSDT'],strategyNames:Object.fromEntries(Object.entries(researchCatalog).map(([k,v])=>[k,v.name])),orderReasons:{},resultGeneration:0,lastSingleEquity:[],lastEquity:[],lastHoldEquity:[],
      $:id=>nodes[id]||(nodes[id]={innerHTML:'',addEventListener(){}}),esc:String,number:String,money:String,cst:String,quantityText:String,drawEquity(){},requestAnimationFrame:fn=>fn(),Number};
    vm.createContext(sandbox);vm.runInContext(fn,sandbox);
    const result={strategy,inst_id:'BTCUSDT',is_multi:item.multi,bars:1000,signal_bars:100,warmup_bars:10,capital:10000,final_equity:10500,return_pct:5,buy_hold_pct:3,max_drawdown_pct:2,order_count:3,completed_trades:1,win_rate_pct:100,cash_usdt:5000,fees_paid:10,turnover_usdt:10000,slippage_cost_usdt:5,realized_pnl_usdt:20,skipped_orders:0,stop_exits:1,rebalances:1,grid_rounds:1,grid_levels:[70,100,130],dca_buys:1,open_position:true,start_ms:100,end_ms:1000,last_signal_unfilled:false,recent_orders:[],equity_curve:[],buy_hold_curve:[],holdings:[{inst_id:'BTCUSDT',final_qty:1,target_weight_pct:40,actual_weight_pct:39}],strategy_params:Object.fromEntries(Object.entries(item.fields).map(([k,f])=>[k,f.default]))};
    sandbox.showResult({id:'a'.repeat(24),state:'done',result});
    assert.match(nodes.results.innerHTML,/模拟滑点成本/);
    assert.match(nodes.results.innerHTML,/benchmarkCoin/);
    assert.ok(nodes.results.innerHTML.includes(item.name));
    assert.ok(!nodes.results.innerHTML.includes('undefined'),strategy);
  }
});
