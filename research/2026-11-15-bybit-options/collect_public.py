#!/usr/bin/env python3
"""Read-only Bybit public snapshot via NAS; no credentials. Run from repository root."""
import json, pathlib, subprocess
OUT=pathlib.Path(__file__).parent
REMOTE=r'''
import httpx,json,time,datetime,concurrent.futures
c=httpx.Client(proxy="http://127.0.0.1:7890",timeout=35)
records=[]
def get(path,params):
 for attempt in range(4):
  try:
   r=c.get("https://api.bybit.com"+path,params=params);r.raise_for_status(); d=r.json()
   if d.get("retCode")==0: return {"path":path,"params":params,"receivedUtc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"payload":d}
   if attempt==3:return {"path":path,"params":params,"payload":d}
  except Exception as e:
   if attempt==3:return {"path":path,"params":params,"error":str(e)}
  time.sleep(attempt+1)
def catalog(base):
 rows=[];cursor=""
 while True:
  p={"category":"option","baseCoin":base,"limit":1000}
  if cursor:p["cursor"]=cursor
  rec=get("/v5/market/instruments-info",p);records.append(rec)
  d=rec.get("payload",{}).get("result",{}); rows.extend(d.get("list",[])); cursor=d.get("nextPageCursor","")
  if not cursor:break
 return base,rows
all_base,all_rows=catalog("All")
catalogs={}
for row in all_rows: catalogs.setdefault(row["baseCoin"],[]).append(row)
actual=sorted(catalogs)
bases=["All"]
now=int(time.time()*1000)
jobs=[]
for b in actual:
 jobs += [("/v5/market/tickers",{"category":"option","baseCoin":b}), ("/v5/market/tickers",{"category":"spot","symbol":b+"USDT"}), ("/v5/market/tickers",{"category":"linear","symbol":b+"USDT"}), ("/v5/market/kline",{"category":"spot","symbol":b+"USDT","interval":"60","limit":1000,"end":now}), ("/v5/market/kline",{"category":"spot","symbol":b+"USDT","interval":"D","limit":100,"end":now}), ("/v5/market/funding/history",{"category":"linear","symbol":b+"USDT","limit":100})]
with concurrent.futures.ThreadPoolExecutor(4) as ex:records.extend(ex.map(lambda z:get(*z),jobs))
print(json.dumps({"collectionUtc":datetime.datetime.now(datetime.timezone.utc).isoformat(),"catalogs":catalogs,"actualBases":actual,"probedBases":bases,"records":records}))
'''
p=subprocess.run(['ssh','nas','podman exec -i bybitbot-worker python -'],input=REMOTE,text=True,capture_output=True,check=True)
data=json.loads(p.stdout)
(OUT/'public_snapshot.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
print(json.dumps({'collectionUtc':data['collectionUtc'],'actualBases':data['actualBases'],'counts':{b:len(x) for b,x in data['catalogs'].items()},'records':len(data['records'])}))
