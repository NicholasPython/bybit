#!/usr/bin/env python3
"""Collect public Bybit SPOT history for modeled options experiments.
No historical option quotes/IV/bid-ask, settlement-index TWAP or margin history.
Uses existing NAS alias, worker Python/httpx and public HTTP proxy; no API keys.
All stored OHLC bars END no later than cutoff. Daily08 additionally stores only
08:00 opening price, fetched from a boundary candle whose other fields are omitted.

Modes
  (default)        backward collect from cutoff to the requested floor
  --fill           extend an existing partial collect backward from its oldest bar
  --analyze-only   re-derive hourly/daily08/manifest from raw pages only

The requested floor START precedes the first bar the exchange actually serves
(2021-07-05T00:00Z requested, first available bar 2021-07-05T12:00Z). The lead is
reported as a source boundary, not as a fetch gap.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,hashlib,json,pathlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parent
START='2021-07-05T00:00:00+00:00'
CUTOFF='2026-11-14T08:00:00+00:00'
HOUR=3600000
SYMBOLS=['BTCUSDT','ETHUSDT']
REMOTE=r'''
import httpx,json,time,datetime,concurrent.futures,threading
JOBS=__JOBS__
lock=threading.Lock()
def emit(x):
 with lock:print(json.dumps(x),flush=True)
def collect(job):
 symbol=job["symbol"];start=job["start"];end=job["end"];page=job["page"]
 client=httpx.Client(proxy="http://127.0.0.1:7890",timeout=40)
 cursor=end
 while cursor>=start:
  params={"category":"spot","symbol":symbol,"interval":"60","limit":1000,"start":start,"end":cursor}
  for attempt in range(5):
   try:
    response=client.get("https://api.bybit.com/v5/market/kline",params=params);response.raise_for_status();payload=response.json()
    if payload.get("retCode")!=0:raise ValueError(str(payload))
    break
   except Exception:
    if attempt==4:raise
    time.sleep(min(12,1.5*2**attempt))
  batch=payload.get("result",{}).get("list",[])
  emit({"type":"page","symbol":symbol,"page":page,"requestedParams":params,"receivedUtc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"serverTime":payload.get("time"),"retCode":payload.get("retCode"),"rows":batch})
  if not batch:break
  oldest=min(int(row[0]) for row in batch)
  if oldest>cursor:raise ValueError("Non-decreasing backward cursor")
  if oldest<=start:break
  cursor=oldest-1;page+=1;time.sleep(.15)
 client.close()
 emit({"type":"done","symbol":symbol})
with concurrent.futures.ThreadPoolExecutor(2) as executor:
 list(executor.map(collect,JOBS))
'''
def stamp(s):return int(dt.datetime.fromisoformat(s).timestamp()*1000)
def iso(ms):return dt.datetime.fromtimestamp(ms/1000,dt.timezone.utc).isoformat()
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def write_csv(path,header,rows):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('w',newline='') as f:
  w=csv.writer(f);w.writerow(header);w.writerows(rows)
def raw_pages(data_dir,symbol):return sorted((data_dir/'raw'/symbol).glob('*.json'))
def page_state(data_dir,symbol):
 """Existing coverage for one symbol: oldest bar, highest page index, rows seen."""
 pages=raw_pages(data_dir,symbol)
 if not pages:return None
 stamps=[];highest=-1;count=0
 for p in pages:
  highest=max(highest,int(p.stem))
  for row in json.loads(p.read_text()).get('rows',[]):
   stamps.append(int(row[0]));count+=1
 if not stamps:return None
 return {'oldest':min(stamps),'newest':max(stamps),'nextPage':highest+1,'pageFiles':len(pages),'rows':count}

def analyze(data_dir,start,cutoff):
 manifest={'requestedStartUtc':iso(start),'cutoffUtc':iso(cutoff),'fetchCompletedUtc':dt.datetime.now(dt.timezone.utc).isoformat(),'source':'https://api.bybit.com/v5/market/kline','parameters':{'category':'spot','interval':'60','limit':1000},'semantics':'timestamp=UTC milliseconds of hourly candle START; hourly last candle END <= cutoff. Daily08 open uses boundary bar open only; signal_close=previous completed 07:00 candle close; previous_daily_close=prior daily08 signal_close. Spot is only a proxy: no option index TWAP, historical option quotes, IV, bid/ask, cross-margin or actual fees history acquired.','symbols':{}}
 for symbol in SYMBOLS:
  pages=[json.loads(p.read_text()) for p in raw_pages(data_dir,symbol)]
  unique={};duplicates=0;conflicts=[];misaligned=[];out_of_range=[]
  for page in pages:
   for row in page['rows']:
    t=int(row[0])
    if t%HOUR:misaligned.append(t)
    if not (start<=t<=cutoff):out_of_range.append(t)
    if t in unique:
     duplicates+=1
     if unique[t]!=row:conflicts.append(t)
    unique[t]=row
  assert not conflicts,('Conflicting duplicate candles',symbol,conflicts)
  assert not misaligned,('Misaligned hourly timestamps',symbol,misaligned[:5])
  assert not out_of_range,('Candles outside requested range',symbol,out_of_range[:5])
  hourly=sorted((r for t,r in unique.items() if t+HOUR<=cutoff),key=lambda r:int(r[0]))
  bad=[]
  for row in hourly:
   o,h,l,c,v,turn=map(float,row[1:7])
   if not (o>0 and l>0 and h>=max(o,c,l) and l<=min(o,c,h) and v>=0 and turn>=0):bad.append(row)
  assert not bad,('Invalid OHLC',symbol,bad[:5])
  stamps=[int(r[0]) for r in hourly]
  assert stamps==sorted(stamps) and len(set(stamps))==len(stamps),('Non-monotonic or repeated hourly stamps',symbol)
  sourceFloor=stamps[0];available=set(stamps)
  # The exchange serves no bar before sourceFloor; that lead is a source boundary, not a fetch gap.
  unavailableLead=[t for t in range(start,sourceFloor,HOUR)]
  expected=set(range(sourceFloor,cutoff,HOUR))
  missing=sorted(expected-available);gaps=[]
  for t in missing:
   if gaps and t==gaps[-1]['endMs']+HOUR:gaps[-1]['endMs']=t;gaps[-1]['count']+=1
   else:gaps.append({'startMs':t,'endMs':t,'count':1})
  for g in gaps:g['startUtc']=iso(g['startMs']);g['endUtc']=iso(g['endMs'])
  # Time boundaries: first/last bar, bar alignment, bar end vs cutoff, stored boundary candle.
  boundaryCandle=unique.get(cutoff)
  hp=data_dir/'hourly'/f'{symbol}.csv';write_csv(hp,['timestamp','open','high','low','close','volume','turnover'],hourly)
  daily=[];missing_anchors=[];previous=None
  first=(sourceFloor//86400000)*86400000+8*HOUR
  for t in range(first,cutoff+1,86400000):
   if t not in unique or t-HOUR not in unique:missing_anchors.append(iso(t));continue
   close=unique[t-HOUR][4];prior=previous[1] if previous and previous[0]==t-86400000 else ''
   daily.append([t,unique[t][1],close,prior]);previous=(t,close)
  dp=data_dir/'daily08'/f'{symbol}.csv';write_csv(dp,['timestamp','open','signal_close','previous_daily_close'],daily)
  # Omit the final candle except its opening price, as no later valuation is authorized.
  for page in pages:
   for row in page['rows']:
    if int(row[0])==cutoff:row[2:]=['OMITTED_AFTER_CUTOFF']*5
   fp=data_dir/'raw'/symbol/f"{page['page']:04d}.json";fp.write_text(json.dumps(page,separators=(',',':'))+'\n')
  manifest['symbols'][symbol]={'pages':len(pages),'rows':len(hourly),'firstCandleStartUtc':iso(sourceFloor),'lastCandleStartUtc':iso(stamps[-1]),'lastCandleEndUtc':iso(stamps[-1]+HOUR),'sourceAvailabilityFloorUtc':iso(sourceFloor),'unavailableBeforeSourceFloorBars':len(unavailableLead),'unavailableBeforeSourceFloorUtc':[iso(unavailableLead[0]),iso(unavailableLead[-1])] if unavailableLead else [],'expectedRows':len(expected),'missingBars':len(missing),'gaps':gaps,'duplicateRows':duplicates,'conflictingDuplicates':len(conflicts),'invalidOHLC':len(bad),'misalignedTimestamps':len(misaligned),'barsOutsideRequestedRange':len(out_of_range),'boundaryCandlePresent':boundaryCandle is not None,'boundaryCandleStartUtc':iso(cutoff) if boundaryCandle else None,'hourlyBarsEndingAfterCutoff':len([t for t in stamps if t+HOUR>cutoff]),'hourlyCsv':str(hp.relative_to(ROOT)),'hourlySha256':digest(hp),'daily08Rows':len(daily),'daily08FirstUtc':iso(int(daily[0][0])),'daily08LastUtc':iso(int(daily[-1][0])),'daily08MissingAnchors':missing_anchors,'daily08Csv':str(dp.relative_to(ROOT)),'daily08Sha256':digest(dp),'firstFetchUtc':min(p['receivedUtc'] for p in pages),'lastFetchUtc':max(p['receivedUtc'] for p in pages)}
 (data_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 return manifest

def main():
 p=argparse.ArgumentParser();p.add_argument('--analyze-only',action='store_true');p.add_argument('--fill',action='store_true');args=p.parse_args()
 data_dir=ROOT/'data';start=stamp(START);cutoff=stamp(CUTOFF)
 if not args.analyze_only:
  jobs=[]
  for symbol in SYMBOLS:
   state=page_state(data_dir,symbol) if args.fill else None
   if state:
    jobs.append({'symbol':symbol,'start':start,'end':state['oldest']-1,'page':state['nextPage']})
    print('Filling',symbol,'from',iso(state['oldest']),'at page',state['nextPage'],flush=True)
   else:
    jobs.append({'symbol':symbol,'start':start,'end':cutoff,'page':0})
  remote=REMOTE.replace('__JOBS__',json.dumps(jobs))
  proc=subprocess.Popen(['ssh','nas','podman exec -i bybitbot-worker python -'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
  proc.stdin.write(remote);proc.stdin.close()
  fetched=0
  for line in proc.stdout:
   page=json.loads(line)
   if page['type']=='done':print('Fetched',page['symbol'],flush=True);continue
   if not page['rows']:
    # Empty batch marks the first bar the exchange serves for this symbol.
    print('No data before',iso(page['requestedParams']['end']),'for',page['symbol'],flush=True);continue
   if int(page['rows'][0][0])==cutoff:
    # Only the boundary candle open is needed; discard post-cutoff information immediately.
    page['rows'][0][2:]=['OMITTED_AFTER_CUTOFF']*5
   path=data_dir/'raw'/page['symbol']/f"{page['page']:04d}.json";path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(page,separators=(',',':'))+'\n')
   fetched+=1
   if fetched%10==0:print(page['symbol'],'page',page['page'],'oldest',iso(min(int(r[0]) for r in page['rows'])),flush=True)
  if proc.wait():raise SystemExit('Remote public-history collection failed; saved partial pages are not a complete dataset.')
 result=analyze(data_dir,start,cutoff)
 for s,d in result['symbols'].items():print(s,json.dumps(d),flush=True)
if __name__=='__main__':main()
