"""Append observation-only Pine instrumentation to the immutable compiled baseline.

Writes a temporary .txt for the editor, never overwrites the one deliverable .pine.
No strategy/order/decision function is added, removed or called by the appendix.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = '48e5cddab17ea831bdc277f17f0406329fdfe39c'
PATH = 'unified/QIRVANTA_UNIFIED.pine'
base = subprocess.check_output(['git', 'show', f'{BASE}:{PATH}'], cwd=ROOT).decode('utf-8')
if hashlib.sha256(base.encode()).hexdigest() != 'c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5':
    raise ValueError('Compiled baseline hash changed')
appendix = r'''
// DIAGNOSTIC OBSERVER ONLY. Baseline above is byte-identical to commit 48e5cdd.
// Fixed already-observed cutoff; no realtime or future bars enter this journal.
int dgCutoff = timestamp("09 Nov 2026 02:45 +0000")
var array<string> dgRows = array.new<string>()
var array<string> dgBars = array.new<string>()
var int dgSeen = 0
var int dgSubmitted = 0
var int dgCount = 0
var int dgFirst = na
var float dgHash1 = 0
var float dgHash2 = 0
string dgRun = "baseline|" + runModel
f_dgHead(string k) => '{"schema":"qirvanta.native.v1","run_id":' + f_q(dgRun) + ',"kind":' + f_q(k)
if barstate.isconfirmed and time_close <= dgCutoff
    dgFirst := na(dgFirst) ? time : dgFirst
    dgCount += 1
    if runModel == "Всички"
        string b = '[' + f_int(time) + ',' + f_int(time_close)
        for v in array.from(open, high, low, close, volume, htfClose, htfEMA50, htfEMA200, htfEMA50prev, priorDayHigh, priorDayLow)
            b += ',' + f_num(v)
        array.push(dgBars, b + ']')
    // Two modular checksums, not a cryptographic dataset hash. Include HTF inputs.
    for v in array.from(float(time), open / syminfo.mintick, high / syminfo.mintick, low / syminfo.mintick, close / syminfo.mintick, volume, htfClose / syminfo.mintick, htfEMA50 / syminfo.mintick, htfEMA200 / syminfo.mintick, htfEMA50prev / syminfo.mintick, priorDayHigh / syminfo.mintick, priorDayLow / syminfo.mintick)
        float q = na(v) ? -999999.0 : math.round(v)
        dgHash1 := (dgHash1 * 131.0 + q) % 1000000007.0
        dgHash2 := (dgHash2 * 137.0 + q) % 1000000009.0
    if nativeAdmittedNow and not na(nativePlan)
        Plan p = nativePlan
        dgSubmitted += 1
        string row = f_dgHead("submitted") + ',"plan_id":' + f_q(p.id) + ',"entry_id":' + f_q(p.id) + ',"submitted_time":' + f_int(time_close) + ',"submitted_bar":' + f_int(bar_index) + ',"quantity":' + f_num(nativeCampaignQty) + ',"family":' + f_int(p.family) + ',"side":' + f_int(p.side) + ',"htf_bias":' + f_int(bullHTF ? 1 : bearHTF ? -1 : 0) + ',"regime":' + f_q(p.countertrend ? "COUNTERTREND" : "TREND") + ',"countertrend":' + str.tostring(p.countertrend) + ',"local_aligned":' + str.tostring(p.localAligned) + ',"efficiency":' + f_num(p.efficiencyValue) + ',"reference":' + f_num(p.reference) + ',"initial_sl":' + f_num(p.initialStop) + ',"atr":' + f_num(p.atrValue) + ',"tp1":' + f_num(p.tp1) + ',"tp2":' + f_num(p.tp2) + ',"tp3":' + f_num(p.tp3) + ',"maxhold":' + f_int(p.maxHoldBarsFrozen) + '}'
        array.push(dgRows, row)
    if strategy.closedtrades > dgSeen
        for j = math.max(dgSeen, nz(strategy.closedtrades.first_index, 0)) to strategy.closedtrades - 1
            string row = f_dgHead("closed") + ',"trade_index":' + f_int(j) + ',"entry_id":' + f_q(strategy.closedtrades.entry_id(j)) + ',"entry_time":' + f_int(strategy.closedtrades.entry_time(j)) + ',"entry_bar_index":' + f_int(strategy.closedtrades.entry_bar_index(j)) + ',"size":' + f_num(strategy.closedtrades.size(j)) + ',"entry_price":' + f_num(strategy.closedtrades.entry_price(j)) + ',"exit_id":' + f_q(strategy.closedtrades.exit_id(j)) + ',"exit_comment":' + f_q(strategy.closedtrades.exit_comment(j)) + ',"exit_time":' + f_int(strategy.closedtrades.exit_time(j)) + ',"exit_bar_index":' + f_int(strategy.closedtrades.exit_bar_index(j)) + ',"exit_price":' + f_num(strategy.closedtrades.exit_price(j)) + ',"profit":' + f_num(strategy.closedtrades.profit(j)) + ',"commission":' + f_num(strategy.closedtrades.commission(j)) + ',"max_drawdown":' + f_num(strategy.closedtrades.max_drawdown(j)) + ',"max_runup":' + f_num(strategy.closedtrades.max_runup(j)) + '}'
            array.push(dgRows, row)
        dgSeen := strategy.closedtrades
    if time_close == dgCutoff
        string run = f_dgHead("run") + ',"symbol":' + f_q(syminfo.tickerid) + ',"timeframe":' + f_q(timeframe.period) + ',"source_sha256":"c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5","account_currency":' + f_q(strategy.account_currency) + ',"symbol_currency":' + f_q(syminfo.currency) + ',"pointvalue":' + f_num(syminfo.pointvalue) + ',"profit_semantics":"UNVERIFIED","first_bar_time":' + f_int(dgFirst) + ',"last_bar_open":' + f_int(time) + ',"cutoff":' + f_int(dgCutoff) + ',"bar_count":' + f_int(dgCount) + ',"checksum1":' + f_num(dgHash1) + ',"checksum2":' + f_num(dgHash2) + ',"mintick":' + f_num(syminfo.mintick) + '}'
        log.info("QIRVANTA_DATA_BEGIN\n" + run)
        string batch = ""
        int batchN = 0
        for row in dgRows
            batch += row + "\n"
            batchN += 1
            if batchN == 10
                log.info(batch)
                batch := ""
                batchN := 0
        if batch != ""
            log.info(batch)
        string openRows = ""
        if strategy.opentrades > 0
            for j = 0 to strategy.opentrades - 1
                openRows += (openRows == "" ? "" : ",") + '{"entry_id":' + f_q(strategy.opentrades.entry_id(j)) + ',"entry_time":' + f_int(strategy.opentrades.entry_time(j)) + ',"entry_bar_index":' + f_int(strategy.opentrades.entry_bar_index(j)) + ',"size":' + f_num(strategy.opentrades.size(j)) + ',"entry_price":' + f_num(strategy.opentrades.entry_price(j)) + '}'
        string snap = f_dgHead("snapshot") + ',"at_time":' + f_int(time_close) + ',"at_bar":' + f_int(bar_index) + ',"closed_count":' + f_int(strategy.closedtrades) + ',"first_closed_index":' + f_int(nz(strategy.closedtrades.first_index, 0)) + ',"submitted_count":' + f_int(dgSubmitted) + ',"open_complete":true,"open":[' + openRows + '],"netprofit":' + f_num(strategy.netprofit) + ',"max_drawdown":' + f_num(strategy.max_drawdown) + '}'
        log.info(snap + "\nQIRVANTA_DATA_END")
        if array.size(dgBars) > 0
            log.info("QIRVANTA_BARS_BEGIN\n" + '{"columns":["time","time_close","open","high","low","close","volume","htfClose","htfEMA50","htfEMA200","htfEMA50prev","priorDayHigh","priorDayLow"],"price_serialization":"8 decimal places","source":"Pine observed OANDA:XAUUSD M15"}')
            string bars = ""
            int barN = 0
            for b in dgBars
                bars += b + "\n"
                barN += 1
                if barN == 100
                    log.info(bars)
                    bars := ""
                    barN := 0
            if bars != ""
                log.info(bars)
            log.info("QIRVANTA_BARS_END")
'''
appendix = appendix.replace('f_q(strategy.closedtrades.exit_comment(j))', 'f_q(na(strategy.closedtrades.exit_comment(j)) ? "" : strategy.closedtrades.exit_comment(j))')
output = Path(sys.argv[1])
if output.suffix.lower() != '.txt':
    raise ValueError('Observer output must be a new .txt file, not a production Pine file')
if output.exists() or output.is_symlink():
    raise ValueError('Refusing to overwrite an existing observer output path')
output.parent.mkdir(parents=True, exist_ok=True)
with output.open('x', encoding='utf-8', newline='\n') as stream:
    stream.write(base + appendix)
print(f'{output}: baseline={len(base)} appendix={len(appendix)} chars')
