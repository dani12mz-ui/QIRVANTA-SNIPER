#!/usr/bin/env python3
"""Create reproducible delivery ZIP, excluding Git metadata, caches and real private data."""
from pathlib import Path
import hashlib,json,zipfile
ROOT=Path(__file__).resolve().parents[1]
def main():
    baseline=ROOT/'baseline/QIRVANTA_SNIPER_v7_6_TIMELY_SIGNALS.pine';data=baseline.read_bytes()
    blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    assert blob=='46263e3c4740e4fc57553b785ccc76b89df32865'
    tests=json.loads((ROOT/'reports/tests.json').read_text());assert tests['successful']
    build=json.loads((ROOT/'reports/build.json').read_text())
    status={'CODE_TESTS':{'status':'PASS_LOCAL_REFERENCE_AND_STATIC_ONLY','tests':tests['tests_run'],'errors':tests['errors'],'failures':tests['failures']},'PINE_COMPILE':{'status':'PINE_COMPILE_UNVERIFIED','reason':'No accessible authorized Pine compiler was available/used'},'EXECUTION_PARITY':{'status':'UNVERIFIED_RUNTIME_WITH_KNOWN_FILL_DIFFERENCES','shared_source_core_sha256':build['canonical_core_sha256']},'OUT_OF_SAMPLE':{'status':'PERFORMANCE_UNVERIFIED_MISSING_DATA'},'FORWARD_TEST':{'status':'NOT_RUN'},'MOBILE_UI':{'status':'HTML_MOCK_CHECKED_TRADINGVIEW_UNVERIFIED','viewports':['390x844','430x932','1440x900']},'GITHUB':{'modified':False,'published':False},'BROKER':{'connected':False,'orders_submitted':False},'production_ready':False,'profitability_demonstrated':False}
    (ROOT/'reports/STATUS.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
    (ROOT/'CONFIG_MANIFEST.json').write_text(json.dumps({'source':'src/inputs.pineinc is canonical; regenerate both scripts with tools/build.py','baseline_commit':'7fe6534f72fa23da18aa9282ee8f5ac11dcc9df6','baseline_git_blob':blob,'core_sha256':build['canonical_core_sha256'],'symbol_provider':'NOT_BOUND: intended user context OANDA:XAUUSD; not equivalent to PU Prime XAUUSD.s','supported_timeframes_seconds':[300,900,1800,3600,14400],'ohlc':'standard only','risk_timezone':'Europe/Sofia, day keyed to BAR OPEN','markets_tested':[],'optimization_trials':0,'actual_market_data_bundled':False,'native_properties_defaults':{'capital':100000,'currency':'USD','total_units':3,'commission_cash_per_contract_per_side':0.175,'slippage_ticks':2,'margin_percent':100,'process_orders_on_close':False,'calc_on_order_fills':False,'bar_magnifier_requested':True},'paper_cost_defaults':{'spread_price_roundtrip':.35,'slip_ticks_per_side':2,'commission_price_roundtrip':0,'funding_price_per_day':0},'unknown_validation':status},ensure_ascii=False,indent=2)+'\n')
    included=[]
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file():continue
        rel=p.relative_to(ROOT)
        if '.git' in rel.parts or '__pycache__' in rel.parts or p.suffix in ('.pyc','.zip'):continue
        if rel.parts[:2] in [('reports','real_run')]:continue
        if str(rel) in ('data/ohlc.csv','data/pine_events.jsonl','data/manifest.json','SHA256SUMS.txt'):continue
        included.append(p)
    hashes='\n'.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.relative_to(ROOT).as_posix() for p in included)+'\n'
    (ROOT/'SHA256SUMS.txt').write_text(hashes)
    included.append(ROOT/'SHA256SUMS.txt')
    dest=ROOT.parent/'QIRVANTA_REFACTORED_DELIVERY.zip'
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for p in included:
            info=zipfile.ZipInfo('QIRVANTA/'+p.relative_to(ROOT).as_posix(),date_time=(2026,11,7,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,p.read_bytes())
    with zipfile.ZipFile(dest) as z:assert z.testzip() is None
    print(json.dumps({'zip':str(dest),'files':len(included),'bytes':dest.stat().st_size,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()},indent=2))
if __name__=='__main__':main()
