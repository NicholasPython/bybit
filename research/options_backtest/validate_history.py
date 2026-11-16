#!/usr/bin/env python3
"""Validate the public spot hourly dataset for BTC/ETH.

Checks, in order:
  A. structure   - row count, UTC hour alignment, strict monotonicity, contiguity
  B. values      - OHLC bounds, non-negative volume/turnover, non-zero prices
  C. provenance  - the hourly CSV is a faithful projection of the raw API pages,
                   duplicate/conflicting timestamps, page-by-page coverage
  D. boundary    - cutoff semantics, the boundary candle, the availability floor
  E. independent - cross-check against research/data/1h/klines (collected by a
                   different script) and against the daily klines snapshot

Standard library only. Reads research/options_backtest/data and research/data.
Writes research/options_backtest/data/validation.json and prints a summary.
"""
from __future__ import annotations
import csv,datetime as dt,json,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[1]          # research/
REPO=ROOT.parents[0]                                       # repository root
BACKTEST=ROOT/'options_backtest'
DATA=BACKTEST/'data'
HOUR=3600000
DAY=86400000
def iso(ms):return dt.datetime.fromtimestamp(int(ms)/1000,dt.timezone.utc).isoformat()
def read_csv(path):
 with path.open() as f:
  r=csv.DictReader(f);return list(r)
def close(a,b,tol=1e-9):
 fa,fb=float(a),float(b)
 return abs(fa-fb)<=tol*max(1.0,abs(fa),abs(fb))
def main():
 report={'checkedUtc':dt.datetime.now(dt.timezone.utc).isoformat(),'symbols':{}}
 manifest=json.loads((DATA/'manifest.json').read_text())
 for symbol in ['BTCUSDT','ETHUSDT']:
  meta=manifest['symbols'][symbol];rows=read_csv(DATA/'hourly'/f'{symbol}.csv')
  stamps=[int(r['timestamp']) for r in rows]
  s={'hourlyRows':len(rows),'firstUtc':iso(stamps[0]),'lastUtc':iso(stamps[-1]),
     'lastBarEndUtc':iso(stamps[-1]+HOUR),'cutoffUtc':manifest['cutoffUtc'],
     'checks':{},'findings':[]}
  # A. structure
  misaligned=[t for t in stamps if t%HOUR]
  dupes=[t for i,t in enumerate(stamps) if i and t==stamps[i-1]]
  unordered=[i for i in range(1,len(stamps)) if stamps[i]<stamps[i-1]]
  steps={stamps[i]-stamps[i-1] for i in range(1,len(stamps))}
  gaps=[];run=None
  for i in range(1,len(stamps)):
   if stamps[i]-stamps[i-1]!=HOUR:
    if run and stamps[i-1]==run[1]:run[1]=stamps[i]
    else:
     if run:gaps.append({'afterUtc':iso(run[1]),'beforeUtc':iso(stamps[i]),'bars':int((stamps[i]-run[1])/HOUR)-1})
     run=[stamps[i-1],stamps[i]]
  if run:gaps.append({'afterUtc':iso(run[1]),'beforeUtc':iso(run[-1]),'bars':0})
  s['checks']['hourAligned']=not misaligned
  s['checks']['strictlyIncreasing']=not dupes and not unordered
  s['checks']['noDuplicateTimestamps']=not dupes
  s['checks']['contiguousNoGaps']=not gaps
  s['checks']['distinctStepSizes']=sorted(steps)
  s['interiorGaps']=gaps
  s['misalignedTimestamps']=len(misaligned)
  s['duplicateTimestamps']=len(dupes)
  # B. values
  bad=[];zero_volume=0
  for r in rows:
   o,h,l,c=float(r['open']),float(r['high']),float(r['low']),float(r['close'])
   v,turn=float(r['volume']),float(r['turnover'])
   if not (o>0 and l>0 and h>=max(o,c,l) and l<=min(o,c,h) and v>=0 and turn>=0):bad.append(r['timestamp'])
   if v==0:zero_volume+=1
  s['checks']['ohlcBoundsValid']=not bad
  s['checks']['positivePrices']=not [r for r in rows if float(r['open'])<=0 or float(r['close'])<=0]
  s['invalidOhlcRows']=len(bad)
  s['zeroVolumeRows']=zero_volume
  # continuity: every bar opens where the previous one closed, i.e. no missing trading time
  breaks=[]
  for i in range(1,len(rows)):
   o=float(rows[i]['open']);pc=float(rows[i-1]['close'])
   if abs(o-pc)>1e-9*max(1.0,abs(o)):breaks.append({'utc':iso(int(rows[i]['timestamp'])),'open':o,'prevClose':pc,'pct':round((o-pc)/pc*100,6)})
  s['openPrevCloseBreaks']={'count':len(breaks),'ofTransitions':len(rows)-1,'maxAbsPct':max((abs(b['pct']) for b in breaks),default=0.0),'sample':breaks[:5]}
  s['checks']['openEqualsPrevCloseExactly']=not breaks
  s['checks']['noPriceGapOver0_5Pct']=not [b for b in breaks if abs(b['pct'])>0.5]
  # C. provenance: rebuild the CSV from the raw pages and compare row for row
  pages=[json.loads(p.read_text()) for p in sorted((DATA/'raw'/symbol).glob('*.json'))]
  unique={};dups=0;conflicts=0
  for page in pages:
   for row in page['rows']:
    t=int(row[0])
    if t in unique:
     dups+=1
     if unique[t]!=row:conflicts+=1
    unique[t]=row
  rebuilt=sorted((r for t,r in unique.items() if t+HOUR<=int(dt.datetime.fromisoformat(manifest['cutoffUtc']).timestamp()*1000)),key=lambda r:int(r[0]))
  same=len(rebuilt)==len(rows) and all(
   int(a['timestamp'])==int(b[0]) and close(a['open'],b[1]) and close(a['high'],b[2]) and close(a['low'],b[3])
   and close(a['close'],b[4]) and close(a['volume'],b[5]) and close(a['turnover'],b[6]) for a,b in zip(rows,rebuilt,strict=True))
  s['checks']['csvMatchesRawPages']=same
  s['rawPages']=len(pages);s['rawRowsUnique']=len(unique)
  s['rawDuplicateRows']=dups;s['rawConflictingDuplicates']=conflicts
  page_cov=[]
  for page in pages:
   ts=[int(r[0]) for r in page['rows']]
   if ts:page_cov.append({'page':page['page'],'rows':len(ts),'newestUtc':iso(max(ts)),'oldestUtc':iso(min(ts))})
  page_cov.sort(key=lambda p:p['page'])
  s['pageCoverage']=page_cov
  # D. boundary
  cutoff=int(dt.datetime.fromisoformat(manifest['cutoffUtc']).timestamp()*1000)
  s['checks']['noBarEndsAfterCutoff']=all(t+HOUR<=cutoff for t in stamps)
  s['checks']['lastBarEndsExactlyAtCutoff']=stamps[-1]+HOUR==cutoff
  s['checks']['boundaryCandleStoredRaw']=cutoff in unique
  boundary=unique.get(cutoff)
  s['boundaryCandleFields']=list(boundary) if boundary else None
  s['checks']['boundaryCandleOpenOnly']=bool(boundary) and boundary[2:]==['OMITTED_AFTER_CUTOFF']*5
  omitted=[t for t,row in unique.items() if row[2:]==['OMITTED_AFTER_CUTOFF']*5]
  s['omittedFieldTimestamps']=[iso(t) for t in sorted(omitted)]
  s['checks']['onlyBoundaryCandleOmitted']=omitted==[cutoff]
  floor=int(dt.datetime.fromisoformat(meta['sourceAvailabilityFloorUtc']).timestamp()*1000)
  s['checks']['startsAtSourceFloor']=stamps[0]==floor
  s['sourceAvailabilityFloorUtc']=meta['sourceAvailabilityFloorUtc']
  s['unavailableBeforeSourceFloorBars']=meta['unavailableBeforeSourceFloorBars']
  s['unavailableBeforeSourceFloorUtc']=meta['unavailableBeforeSourceFloorUtc']
  # E. independent cross-checks
  xref={}
  one_hour=ROOT/'data'/'1h'/'klines'/f'{symbol}.csv'
  if one_hour.exists():
   other=read_csv(one_hour);other_map={int(r['timestamp']):r for r in other}
   mine=set(stamps);theirs=set(other_map)
   common=sorted(mine&theirs);diffs=[]
   mine_map={int(r['timestamp']):r for r in rows}
   for t in common:
    a,b=mine_map[t],other_map[t]
    if not (close(a['open'],b['open']) and close(a['high'],b['high']) and close(a['low'],b['low'])
            and close(a['close'],b['close']) and close(a['volume'],b['volume']) and close(a['turnover'],b['turnover'])):
     diffs.append(t)
   their_stamps=sorted(theirs);their_gaps=[]
   for i in range(1,len(their_stamps)):
    if their_stamps[i]-their_stamps[i-1]!=HOUR:their_gaps.append(iso(their_stamps[i-1]))
   xref['oneHourDataset']={'path':str(one_hour.relative_to(REPO)),'rows':len(other),'firstUtc':iso(their_stamps[0]),'lastUtc':iso(their_stamps[-1]),
    'commonBars':len(common),'onlyInNew':len(mine-theirs),'onlyInOld':len(theirs-mine),'valueMismatches':len(diffs),
    'valueMismatchSample':[iso(t) for t in diffs[:5]],'oldDatasetGaps':their_gaps,'oldDatasetContiguous':not their_gaps}
  daily_src=ROOT/'data'/'klines'/f'{symbol}.csv'
  if daily_src.exists():
   daily=read_csv(daily_src);daily_map={int(r['timestamp']):r for r in daily}
   hourly_by_day={}
   for r in rows:
    t=int(r['timestamp']);hourly_by_day.setdefault(t//DAY*DAY,[]).append(r)
   compared=0;mism=[]
   for day,hrs in hourly_by_day.items():
    d=daily_map.get(day)
    if d is None or len(hrs)!=24:continue
    compared+=1
    agg={'open':hrs[0]['open'],'high':max(float(h['high']) for h in hrs),'low':min(float(h['low']) for h in hrs),'close':hrs[-1]['close']}
    if not (close(agg['open'],d['open']) and close(agg['high'],d['high']) and close(agg['low'],d['low']) and close(agg['close'],d['close'])):
     mism.append({'day':iso(day),'hourly':agg,'daily':{'open':d['open'],'high':d['high'],'low':d['low'],'close':d['close']}})
   xref['dailyDataset']={'path':str(daily_src.relative_to(REPO)),'rows':len(daily),'fullDaysCompared':compared,'mismatches':len(mism),'mismatchSample':mism[:3]}
  s['crossChecks']=xref
  report['symbols'][symbol]=s
 (DATA/'validation.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
 for symbol,s in report['symbols'].items():
  print('='*70);print(symbol,s['hourlyRows'],'bars',s['firstUtc'],'->',s['lastUtc'])
  for k,v in s['checks'].items():print(f'  {"PASS" if v is True else "FAIL" if v is False else "----"}  {k}: {v}')
  print('  interior gaps:',len(s['interiorGaps']),'| raw dup rows:',s['rawDuplicateRows'],'| raw conflicting dups:',s['rawConflictingDuplicates'],'| invalid OHLC:',s['invalidOhlcRows'],'| zero-volume rows:',s['zeroVolumeRows'])
  print('  availability floor:',s['sourceAvailabilityFloorUtc'],'| unavailable lead bars:',s['unavailableBeforeSourceFloorBars'],s['unavailableBeforeSourceFloorUtc'])
  for name,x in s.get('crossChecks',{}).items():print('  cross-check',name,json.dumps(x,ensure_ascii=False))
if __name__=='__main__':main()
