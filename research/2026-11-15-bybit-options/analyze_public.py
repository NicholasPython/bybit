#!/usr/bin/env python3
"""Summarize public snapshot; hourly RV excludes unfinished candles; no backtest."""
import json, math, statistics, datetime as dt, collections,pathlib
P=pathlib.Path(__file__).parent
x=json.loads((P/'public_snapshot.json').read_text()); now=dt.datetime.fromisoformat(x['collectionUtc']); ms=now.timestamp()*1000

def rec(path,category,b):
 return next((r.get('payload',{}).get('result',{}) for r in x['records'] if r['path']==path and r['params'].get('category')==category and (r['params'].get('baseCoin')==b or r['params'].get('symbol')==b+'USDT') and (path!='/v5/market/kline' or r['params']['interval']=='60')), {})
def num(o,k):
 try:return float(o.get(k) or 0)
 except:return 0
out={'timestampUtc':now.isoformat(),'timestampBeijing':now.astimezone(dt.timezone(dt.timedelta(hours=8))).isoformat(),'bases':{},'feeAssumption':'Illustration only: taker 0.03% of underlying notional capped at 7% premium, subject to official verification and short-dated discounts.'}
for b,inst in x['catalogs'].items():
 inst=[i for i in inst if i['status']=='Trading']; meta={i['symbol']:i for i in inst}; ts=rec('/v5/market/tickers','option',b).get('list',[]); ts=[t for t in ts if t['symbol'] in meta]
 spot=rec('/v5/market/tickers','spot',b).get('list',[]); linear=rec('/v5/market/tickers','linear',b).get('list',[])
 price=num(spot[0],'lastPrice') if spot else (num(ts[0],'indexPrice') if ts else 0)
 expiries=sorted(set(int(i['deliveryTime']) for i in inst)); quantities=sorted(set((i['lotSizeFilter']['minOrderQty'],i['lotSizeFilter']['qtyStep']) for i in inst))
 k=sorted(rec('/v5/market/kline','spot',b).get('list',[]), key=lambda r:int(r[0])); k=[r for r in k if int(r[0])+3600000<=ms]
 market={'spotPrice':price,'priceSource':'spot lastPrice' if spot else 'option indexPrice (not spot)','spot24hPct':num(spot[0],'price24hPcnt')*100 if spot else None,'fundingRatePct':num(linear[0],'fundingRate')*100 if linear else None,'rv':{}}
 for days in [7,30]:
  v=[r for r in k if int(r[0])>=ms-days*86400000-3600000]
  returns=[math.log(float(v[j][4])/float(v[j-1][4])) for j in range(1,len(v))]
  closes=[float(z[4]) for z in v]; peak=0; mdd=0
  for close in closes:peak=max(peak,close);mdd=min(mdd,close/peak-1)
  market['rv'][str(days)]={'hourlyReturnsN':len(returns),'annualizedPct':statistics.stdev(returns)*math.sqrt(24*365)*100 if len(returns)>1 else None,'closeReturnPct':(closes[-1]/closes[0]-1)*100 if closes else None,'maxDrawdownPct':mdd*100 if closes else None}
 reps=[]
 for target in [7,30]:
  if not expiries:continue
  expiry=min(expiries,key=lambda z:abs((z-ms)/86400000-target))
  for side in ['Call','Put']:
   candidates=[t for t in ts if int(meta[t['symbol']]['deliveryTime'])==expiry and meta[t['symbol']]['optionsType']==side]
   if not candidates:continue
   t=min(candidates,key=lambda z:abs(float(z['symbol'].split('-')[2])-price)); i=meta[t['symbol']]; q=float(i['lotSizeFilter']['minOrderQty']); bid=num(t,'bid1Price');ask=num(t,'ask1Price');mid=(bid+ask)/2; fee=min(price*q*.0003,ask*q*.07) if ask else None
   reps.append({'targetDays':target,'symbol':t['symbol'],'daysToExpiry':(expiry-ms)/86400000,'expiryUtc':dt.datetime.fromtimestamp(expiry/1000,dt.timezone.utc).isoformat(),'minQty':q,'spotNotional':price*q,'strike':float(t['symbol'].split('-')[2]),'bid':bid,'ask':ask,'bidSize':num(t,'bid1Size'),'askSize':num(t,'ask1Size'),'twoSided':bid>0 and ask>0 and num(t,'bid1Size')>=q and num(t,'ask1Size')>=q,'spreadPctMid':(ask-bid)/mid*100 if bid>0 and ask>0 else None,'minimumBuyPremium':ask*q if ask>0 and num(t,'ask1Size')>=q else None,'illustrativeBuyFee':fee,'bidIvPct':num(t,'bid1Iv')*100,'askIvPct':num(t,'ask1Iv')*100,'markIvPct':num(t,'markIv')*100,'delta':num(t,'delta'),'openInterest':num(t,'openInterest'),'volume24h':num(t,'volume24h'),'markPrice':num(t,'markPrice')})
 out['bases'][b]={'count':len(inst),'callPut':dict(collections.Counter(i['optionsType'] for i in inst)),'quoteCoins':sorted(set(i['quoteCoin'] for i in inst)),'settleCoins':sorted(set(i['settleCoin'] for i in inst)),'minQtyAndStep':quantities,'metadataKeys':list(inst[0]) if inst else [],'expiryLadderUtc':[{ 'time':dt.datetime.fromtimestamp(z/1000,dt.timezone.utc).isoformat(),'count':sum(int(i['deliveryTime'])==z for i in inst)} for z in expiries],'market':market,'liquidity':{'tickers':len(ts),'twoSidedPositiveQuotes':sum(num(t,'bid1Price')>0 and num(t,'ask1Price')>0 for t in ts),'zero24hVolume':sum(num(t,'volume24h')==0 for t in ts),'zeroOI':sum(num(t,'openInterest')==0 for t in ts),'totalVolume24hUnderlying':sum(num(t,'volume24h') for t in ts)},'representatives':reps}
(P/'analysis.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
for b,d in out['bases'].items():
 print(b,d['count'],d['minQtyAndStep'],d['quoteCoins'],d['expiryLadderUtc'][0],d['expiryLadderUtc'][-1],d['market'],d['liquidity'])
 for r in d['representatives']:print('REP',json.dumps(r))
