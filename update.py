import json, os, math, re, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT=Path(__file__).resolve().parents[1]
SITE=ROOT/'site'; DATA=SITE/'data.json'
CN=timezone(timedelta(hours=8))


def num(v):
    try:
        if v is None or str(v).strip()=='' or str(v).lower()=='nan': return None
        return float(str(v).replace(',','').replace('%',''))
    except Exception: return None

def pick(df,names):
    for n in names:
        if n in df.columns: return n
    return None

def retry(fn, attempts=3, sleep=2):
    last=None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last=e
            if i < attempts-1: time.sleep(sleep*(i+1))
    raise last

def finite(v):
    return v is not None and math.isfinite(v)

def _contract_rows(df, prefix="RU"):
    """Normalize live Chinese futures quotes into contract rows."""
    nc=pick(df,['symbol','合约','品种','contract'])
    pc=pick(df,['current_price','最新价','现价','price'])
    oc=pick(df,['hold','open_interest','持仓量','position'])
    vc=pick(df,['volume','成交量','vol'])
    lc=pick(df,['last_settle_price','last_close','前结算价','昨结算'])
    if not nc or not pc:
        raise RuntimeError(f'{prefix}: realtime quote columns unavailable')
    x=df[df[nc].astype(str).str.upper().str.match(rf'{prefix}\d{{4}}$',na=False)].copy()
    if x.empty:
        # Some AKShare endpoints return names with exchange suffixes.
        x=df[df[nc].astype(str).str.upper().str.contains(prefix,na=False)].copy()
        x=x[x[nc].astype(str).str.upper().str.contains(r'\d{4}',regex=True,na=False)]
    rows=[]
    for _,r in x.iterrows():
        sym=str(r[nc]).upper()
        m=re.search(r'(\d{4})',sym)
        if not m: continue
        rows.append({
            'symbol':sym,'price':num(r[pc]),
            'oi':num(r[oc]) if oc else None,
            'volume':num(r[vc]) if vc else None,
            'last_settle':num(r[lc]) if lc else None
        })
    return rows

def _choose_main(rows):
    """Main contract = highest positive open interest; fallback to highest volume."""
    valid=[r for r in rows if r['price'] is not None]
    oi=[r for r in valid if r['oi'] is not None]
    if oi:
        return max(oi,key=lambda r:r['oi'])
    vol=[r for r in valid if r['volume'] is not None]
    if vol: return max(vol,key=lambda r:r['volume'])
    return max(valid,key=lambda r:r['symbol'])

def _historical_main_series(symbol='RU0'):
    import akshare as ak
    df=retry(lambda: ak.futures_zh_daily_sina(symbol=symbol))
    dc=pick(df,['date','日期'])
    cc=pick(df,['close','收盘价'])
    oc=pick(df,['hold','open_interest','持仓量','持仓量(手)','position'])
    vc=pick(df,['volume','成交量','vol'])
    if not cc: raise RuntimeError('historical price column unavailable')
    # Keep rows in source order; AKShare normally returns chronological order.
    out=[]
    for i,(_,r) in enumerate(df.iterrows()):
        p=num(r[cc])
        if p is None: continue
        out.append({
            'date':str(r[dc]) if dc else '',
            'price':p,
            'oi':num(r[oc]) if oc else None,
            'volume':num(r[vc]) if vc else None
        })
    return out

def market():
    import akshare as ak
    spot=retry(lambda: ak.futures_zh_spot(subscribe_list=['RU','BR'], market='CF'))
    rurows=_contract_rows(spot,'RU')
    if not rurows: raise RuntimeError('RU realtime contracts unavailable')
    main=_choose_main(rurows)
    brrows=_contract_rows(spot,'BR')
    brmain=_choose_main(brrows) if brrows else {'price':None}
    last=main.get('last_settle')
    ch=((main['price']/last-1)*100) if main['price'] and last else None

    # Fetch the actual main contract's daily history to obtain yesterday's OI/volume.
    oi_change=vol_change=None
    try:
        h=retry(lambda: ak.futures_zh_daily_sina(symbol=main['symbol']))
        hc=pick(h,['close','收盘价']); ho=pick(h,['hold','open_interest','持仓量','持仓量(手)','position'])
        hv=pick(h,['volume','成交量','vol'])
        if len(h)>=2 and ho:
            a=num(h.iloc[-1][ho]); b=num(h.iloc[-2][ho])
            if a is not None and b not in (None,0): oi_change=(a/b-1)*100
        if len(h)>=2 and hv:
            a=num(h.iloc[-1][hv]); b=num(h.iloc[-2][hv])
            if a is not None and b not in (None,0): vol_change=(a/b-1)*100
    except Exception:
        pass

    return {
        'price':main['price'],'change_pct':ch,
        'main_contract':main['symbol'],
        'main_oi':main.get('oi'),'main_volume':main.get('volume'),
        'main_last_settle':last,
        'oi_change_pct':oi_change,'volume_change_pct':vol_change,
        'br_price':brmain.get('price'),'br_change_pct':None,
        'source':'AKShare/Sina 实时主力 + 主力历史'
    }

def trends():
    hist=_historical_main_series('RU0')
    if len(hist)<61: raise RuntimeError('RU historical series < 61 observations')
    p=[x['price'] for x in hist]
    latest=hist[-1]
    result={
        'trend20':(p[-1]/p[-21]-1)*100,
        'trend60':(p[-1]/p[-61]-1)*100,
        'hist_latest_oi':latest.get('oi'),
        'hist_latest_volume':latest.get('volume')
    }
    # Historical RU0 OI/volume are retained for diagnostics and 1-day confirmation.
    if len(hist)>=2:
        prev=hist[-2]
        if latest.get('oi') is not None and prev.get('oi') not in (None,0):
            result['oi_change_pct']=(latest['oi']/prev['oi']-1)*100
        else: result['oi_change_pct']=None
        if latest.get('volume') is not None and prev.get('volume') not in (None,0):
            result['volume_change_pct']=(latest['volume']/prev['volume']-1)*100
        else: result['volume_change_pct']=None
    return result

def qingdao_inventory():
    """Scrape the latest public Oilchem rubber page/article. Falls back to env if blocked."""
    import requests
    from bs4 import BeautifulSoup
    url='https://rubb.oilchem.net/rubber/Naturalrubber.shtml'
    headers={'User-Agent':'Mozilla/5.0 (compatible; RUBRU-V4/1.0)'}
    html=requests.get(url,headers=headers,timeout=20).text
    soup=BeautifulSoup(html,'html.parser')
    text=soup.get_text(' ',strip=True)
    # First try page text, then latest article links whose titles mention inventory/report.
    patterns=[
        r'青岛港天然橡胶库存(?:为|：)\s*([0-9,]+)\s*吨\s*[（(]\s*([+\-]?[0-9,]+)',
        r'青岛地区.*?库存(?:为|：)\s*([0-9,]+)\s*吨\s*[（(]\s*([+\-]?[0-9,]+)'
    ]
    for p in patterns:
        m=re.search(p,text)
        if m:
            level=num(m.group(1)); delta=num(m.group(2));
            return {'inventory_t':level,'inventory_delta_t':delta,'inventory_change_pct':(delta/level*100 if level else None),'source':'隆众资讯公开页面'}
    # Follow likely latest inventory links and parse article body.
    for a in soup.find_all('a',href=True):
        title=a.get_text(' ',strip=True)
        if '库存' in title and ('青岛' in title or '天然橡胶' in title):
            href=a['href']
            if href.startswith('//'): href='https:'+href
            elif href.startswith('/'): href='https://www.oilchem.net'+href
            elif href.startswith('26-'): href='https://www.oilchem.net/'+href
            try:
                rr=requests.get(href,headers=headers,timeout=20)
                tt=BeautifulSoup(rr.text,'html.parser').get_text(' ',strip=True)
                for p in patterns:
                    m=re.search(p,tt)
                    if m:
                        level=num(m.group(1)); delta=num(m.group(2));
                        return {'inventory_t':level,'inventory_delta_t':delta,'inventory_change_pct':(delta/level*100 if level else None),'source':'隆众资讯公开页面'}
            except Exception: pass
    raise RuntimeError('Qingdao inventory parser found no current value')

def sgx_data():
    import akshare as ak
    # SGX settlement data are published on the next Singapore business day.
    d=datetime.now(CN).date()
    last=None
    for i in range(1,6):
        day=d-timedelta(days=i)
        try:
            df=ak.futures_settlement_price_sgx(date=day.strftime('%Y%m%d'))
            if df is not None and len(df): last=df; break
        except Exception: pass
    if last is None: raise RuntimeError('SGX settlement unavailable')
    com=pick(last,['COM']); settle=pick(last,['SETTLE']); series=pick(last,['SERIES'])
    x=last[last[com].astype(str).str.contains('TF',case=False,na=False)].copy()
    if x.empty: raise RuntimeError('SGX TSR20/TF not found')
    # nearest non-expired TSR20 contract = smallest positive months distance in COM_MM/COM_YY
    x['settle_num']=x[settle].apply(num)
    x=x[x['settle_num'].notna()]
    x=x.sort_values(['COM_YY','COM_MM'])
    row=x.iloc[0]
    current=num(row[settle]);
    # previous trading day comparison from a second settlement pull
    prev=None
    for i in range(2,8):
        day=d-timedelta(days=i)
        try:
            pdf=ak.futures_settlement_price_sgx(date=day.strftime('%Y%m%d'))
            px=pdf[pdf[com].astype(str).str.contains('TF',case=False,na=False)]
            if not px.empty:
                vals=[num(v) for v in px[settle].tolist() if num(v) is not None]
                if vals: prev=vals[0]; break
        except Exception: pass
    ch=((current/prev-1)*100) if current and prev else None
    return {'tsr20_uscent_kg':current,'change_pct':ch,'contract':str(row[series]) if series else 'TF','source':'SGX/AKShare'}

def ru_month_spread():
    import akshare as ak
    # Use realtime RU contract quotes and explicitly compute RU11-RU01 when both exist.
    df=retry(lambda: ak.futures_zh_spot(subscribe_list=['RU'],market='CF'))
    nc=pick(df,['symbol','合约','品种']); pc=pick(df,['current_price','最新价','现价'])
    rows=df[df[nc].astype(str).str.match(r'RU\d{4}',na=False)].copy()
    vals={str(r[nc]).upper():num(r[pc]) for _,r in rows.iterrows()}
    def get(mm):
        matches=[(k,v) for k,v in vals.items() if re.fullmatch(r'RU\d{2}'+mm,k)]
        return matches[0][1] if matches else None
    p11=get('11'); p01=get('01')
    if p11 is not None and p01 is not None:
        return {'ru11':p11,'ru01':p01,'spread_ru11_01':p11-p01,'source':'AKShare/Sina'}
    # fallback: use the nearest available RU contract vs next January.
    jan=[(k,v) for k,v in vals.items() if k.endswith('01') and v is not None]
    avail=[(k,v) for k,v in vals.items() if v is not None]
    if jan and avail:
        nearest=sorted(avail,key=lambda z:z[0])[0]
        return {'ru11':nearest[1],'ru01':jan[0][1],'spread_ru11_01':nearest[1]-jan[0][1],'source':'AKShare/Sina fallback'}
    raise RuntimeError('RU month spread unavailable')

def import_profit(sgx):
    """Estimated Thai TSR20 import margin. Components are auto-fetched where possible; freight/tax are configurable."""
    import requests
    # USD/CNY from a public FX endpoint; fallback to env.
    fx=num(os.getenv('USD_CNY'))
    if fx is None:
        try:
            import akshare as ak
            fxdf=ak.currency_boc_sina(symbol='美元', start_date=datetime.now(CN).strftime('%Y%m%d'), end_date=datetime.now(CN).strftime('%Y%m%d'))
            c=pick(fxdf,['现汇买入价','现汇卖出价','中间价'])
            if c: fx=num(fxdf.iloc[-1][c])
        except Exception: pass
    if fx is None: fx=7.10
    freight=num(os.getenv('RU_IMPORT_FREIGHT_USD')) or 60
    tariff=num(os.getenv('RU_IMPORT_TARIFF')) or 0
    vat=num(os.getenv('RU_IMPORT_VAT'))
    if vat is None: vat=0.13
    port=num(os.getenv('RU_IMPORT_PORT_RMB')) or 100
    # TSR20 is USD cents/kg = USD/t / 10.
    usd_t=(sgx['tsr20_uscent_kg']*10) if sgx.get('tsr20_uscent_kg') is not None else None
    if usd_t is None: raise RuntimeError('No SGX price for import estimate')
    cif_rmb=(usd_t+freight)*fx
    landed_pre_vat=cif_rmb*(1+tariff)+port
    landed=landed_pre_vat*(1+vat)
    # Compare against Qingdao domestic spot if supplied; otherwise use RU reference price later.
    domestic=num(os.getenv('RU_IMPORT_DOMESTIC_SPOT'))
    profit=(domestic-landed) if domestic is not None else None
    return {'import_profit':profit,'estimated_landed_cost':landed,'fx':fx,'freight_usd_t':freight,'tariff':tariff,'vat':vat,'source':'估算：SGX TSR20+汇率+运费+税费'}

def br_change(ru):
    # Compare BR continuous daily history, independent from realtime symbol availability.
    try:
        import akshare as ak
        df=retry(lambda: ak.futures_zh_daily_sina(symbol='BR0'))
        c=pick(df,['close','收盘价'])
        vals=[num(x) for x in df[c].tolist() if num(x) is not None]
        if len(vals)>=2:return (vals[-1]/vals[-2]-1)*100
    except Exception: pass
    return None

def position_calc():
    # User-editable risk calculator parameters. No broker-specific margin is assumed.
    equity=num(os.getenv('ACCOUNT_EQUITY')) or 100000
    risk_pct=num(os.getenv('RISK_PCT')) or 0.75
    entry=num(os.getenv('ENTRY_PRICE'))
    stop=num(os.getenv('STOP_PRICE'))
    multiplier=num(os.getenv('RU_CONTRACT_MULTIPLIER')) or 10
    margin_rate=num(os.getenv('RU_MARGIN_RATE')) or 0.12
    if entry is None or stop is None or entry<=stop:
        return {'equity':equity,'risk_pct':risk_pct,'risk_budget':equity*risk_pct/100,'entry':entry,'stop':stop,'multiplier':multiplier,'margin_rate':margin_rate,'risk_per_lot':None,'contracts':0,'margin_used':0}
    risk_budget=equity*risk_pct/100
    risk_per_lot=(entry-stop)*multiplier
    contracts=max(0,math.floor(risk_budget/risk_per_lot)) if risk_per_lot>0 else 0
    margin_used=contracts*entry*multiplier*margin_rate
    return {'equity':equity,'risk_pct':risk_pct,'risk_budget':risk_budget,'entry':entry,'stop':stop,'multiplier':multiplier,'margin_rate':margin_rate,'risk_per_lot':risk_per_lot,'contracts':contracts,'margin_used':margin_used}

def score(ru,ind,ext):
    """
    T = -4..+4, deliberately fixed to the original four-point structure:
      T1 20-day trend
      T2 60-day trend
      T3 daily price direction
      T4 price + OI structure (volume used as secondary confirmation)
    This keeps T comparable across history.
    """
    t=0
    t20=ru.get('trend20'); t60=ru.get('trend60'); ch=ru.get('change_pct')
    oi=ru.get('oi_change_pct')
    vol=ru.get('volume_change_pct')

    # T1/T2: medium-term trend
    if t20 is not None: t += 1 if t20>3 else -1 if t20<-3 else 0
    if t60 is not None: t += 1 if t60>6 else -1 if t60<-6 else 0
    # T3: current-session direction
    if ch is not None: t += 1 if ch>1 else -1 if ch<-1 else 0
    # T4: price/OI structure; volume breaks ties / strengthens confirmation.
    if ch is not None and oi is not None:
        if ch>0 and oi>2: t += 1
        elif ch<0 and oi>2: t -= 1
        elif ch>1 and oi<=-2:
            t += 1 if vol is not None and vol>10 else 0
        elif ch<-1 and oi<=-2:
            t -= 1 if vol is not None and vol>10 else 0

    F=0; inv=ind.get('inventory_change_pct'); imp=ind.get('import_profit'); sp=ind.get('spread_ru11_01')
    if inv is not None: F += 2 if inv<-2 else 1 if inv<0 else -2 if inv>2 else -1 if inv>0 else 0
    if imp is not None: F += 1 if imp<-300 else -1 if imp>500 else 0
    if sp is not None: F += 1 if sp>-500 else -1 if sp<-1000 else 0

    C=0; sgx=ext.get('sgx_change_pct'); br=ext.get('br_change_pct')
    if sgx is not None: C += 1 if sgx>1 else -1 if sgx<-1 else 0
    if br is not None: C += 1 if br>1 else -1 if br<-1 else 0

    total=t+F+C
    if t>=3 and total>=6: state='加速'
    elif t>=1 and total>=4: state='确认'
    elif t>=1 and total>=1: state='启动'
    elif t<=-3 and total<=-6: state='反转'
    elif t<=-1 and total<=-4: state='衰减'
    else: state='震荡/观察'
    bias='强多' if total>=8 else '偏多' if total>=4 else '强空' if total<=-8 else '偏空' if total<=-4 else '中性'
    action='只考虑回踩做多' if t>=1 and total>=4 else '只考虑反弹做空' if t<=-1 and total<=-4 else '多头观察' if total>=2 else '空头观察' if total<=-2 else '等待'

    structure = '价涨仓增' if ch is not None and oi is not None and ch>0 and oi>2 else \
                '价跌仓增' if ch is not None and oi is not None and ch<0 and oi>2 else \
                '价涨仓减' if ch is not None and oi is not None and ch>0 and oi<-2 else \
                '价跌仓减' if ch is not None and oi is not None and ch<0 and oi<-2 else '中性/待确认'

    parts=[f'趋势结构 T={t:+d}/4（20日趋势、60日趋势、当日价格、价仓结构）。']
    if ru.get('main_contract'): parts.append(f"主力 {ru['main_contract']}，持仓 {ru.get('main_oi','--')} 手，成交量 {ru.get('main_volume','--')} 手，结构：{structure}。")
    if inv is not None: parts.append('青岛库存下降，库存端偏利多。' if inv<0 else '青岛库存上升，库存端偏利空。')
    if imp is not None: parts.append('进口窗口偏关闭。' if imp<0 else '进口利润为正，需防进口补充。')
    if sgx is not None: parts.append('SGX同步走强。' if sgx>0 else 'SGX走弱，外盘确认不足。')
    if sp is not None: parts.append(f'RU11-01月差 {sp:+.0f} 元/吨。')
    parts.append(f'当前为{state}阶段，操作倾向：{action}。')
    # Audit trail: every point must be traceable to a named rule.
    components={
        't20': 1 if t20 is not None and t20>3 else -1 if t20 is not None and t20<-3 else 0,
        't60': 1 if t60 is not None and t60>6 else -1 if t60 is not None and t60<-6 else 0,
        't_day': 1 if ch is not None and ch>1 else -1 if ch is not None and ch<-1 else 0,
        't_oi': 1 if ch is not None and oi is not None and ch>0 and oi>2 else -1 if ch is not None and oi is not None and ch<0 and oi>2 else 0,
        'f_inventory': 2 if inv is not None and inv<-2 else 1 if inv is not None and inv<0 else -2 if inv is not None and inv>2 else -1 if inv is not None and inv>0 else 0,
        'f_import': 1 if imp is not None and imp<-300 else -1 if imp is not None and imp>500 else 0,
        'f_spread': 1 if sp is not None and sp>-500 else -1 if sp is not None and sp<-1000 else 0,
        'c_sgx': 1 if sgx is not None and sgx>1 else -1 if sgx is not None and sgx<-1 else 0,
        'c_br': 1 if br is not None and br>1 else -1 if br is not None and br<-1 else 0
    }
    return {'trend':t,'industry':F,'external':C,'total':total,'state':state,'bias':bias,'action':action,
            'commentary':' '.join(parts),'price_structure':structure,'components':components,
            'missing_core': [k for k,v in components.items() if v==0]}

def load_old():
    try:return json.loads(DATA.read_text(encoding='utf-8'))
    except Exception:return {}

def safe(fn,default):
    try:return fn()
    except Exception as e:
        default=dict(default); default['error']=str(e); return default

def main():
    ru=safe(market,{'price':None,'change_pct':None,'br_price':None,'br_change_pct':None,'source':'market failed'})
    ru.update(safe(trends,{'trend20':None,'trend60':None}))
    ext_sgx=safe(sgx_data,{'tsr20_uscent_kg':None,'change_pct':None,'contract':None,'source':'SGX failed'})
    ru['br_change_pct']=safe(lambda:br_change(ru),None)
    qd=safe(qingdao_inventory,{'inventory_t':None,'inventory_delta_t':None,'inventory_change_pct':None,'source':'Qingdao failed'})
    spread=safe(ru_month_spread,{'ru11':None,'ru01':None,'spread_ru11_01':None,'source':'spread failed'})
    imp=safe(lambda:import_profit(ext_sgx),{'import_profit':None,'estimated_landed_cost':None,'fx':None,'freight_usd_t':None,'tariff':None,'vat':None,'source':'import profit failed'})
    # If domestic spot not manually supplied, use current RU price as a conservative proxy and mark it estimated.
    if imp.get('import_profit') is None and imp.get('estimated_landed_cost') is not None and ru.get('price') is not None:
        imp['import_profit']=ru['price']-imp['estimated_landed_cost']; imp['domestic_basis']='RU reference proxy'
    ind={'inventory_t':qd.get('inventory_t'),'inventory_delta_t':qd.get('inventory_delta_t'),'inventory_change_pct':qd.get('inventory_change_pct'),
         'import_profit':imp.get('import_profit'),'import_landed_cost':imp.get('estimated_landed_cost'),'spread_ru11_01':spread.get('spread_ru11_01'),'ru11':spread.get('ru11'),'ru01':spread.get('ru01')}
    ext={'sgx_change_pct':ext_sgx.get('change_pct'),'sgx_tsr20_uscent_kg':ext_sgx.get('tsr20_uscent_kg'),'sgx_contract':ext_sgx.get('contract'),'br_change_pct':ru.get('br_change_pct')}
    s=score(ru,ind,ext); pos=position_calc(); now=datetime.now(CN).strftime('%Y-%m-%d %H:%M:%S')
    old=load_old(); hist=old.get('history',[]); hist.append({'date':now[:10],'total':s['total']}); hist=hist[-120:]
    fields=[ru['price'],ru['change_pct'],ru['trend20'],ru['trend60'],qd.get('inventory_change_pct'),imp.get('import_profit'),spread.get('spread_ru11_01'),ext_sgx.get('change_pct'),ru.get('br_change_pct')]
    comp=round(sum(x is not None for x in fields)/len(fields)*100)
    out={'meta':{'updated':now,'source':ru['source'],'completeness':comp},'ru':ru,'industry':ind,'external':ext,'score':s,'position':pos,'history':hist,
         'details':{'qingdao_source':qd.get('source'),'import_source':imp.get('source'),'spread_source':spread.get('source'),'sgx_source':ext_sgx.get('source'),'warnings':[v.get('error') for v in [qd,spread,imp,ext_sgx] if isinstance(v,dict) and v.get('error')]+warnings}}
    DATA.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(out,ensure_ascii=False))

if __name__=='__main__': main()
