import unittest
from dataclasses import replace
from tools.ref_engine import *

T=1735732800000  # synthetic fixture timestamp; NOT XAUUSD historical data

def bar(i,o=100,h=104,l=96,c=101):
    return Bar(i,T+i*900000,T+(i+1)*900000,o,h,l,c)
def plan(family=1,side=1,pid='p',signal=0,**kw):
    values=dict(id=pid,family=family,side=side,signal_bar=signal,signal_time=bar(signal).time_close,reference=100,stop=90 if side==1 else 110,targets=(110,120,130) if side==1 else (90,80,70),atr=10,cost_price=.3)
    values.update(kw)
    return Plan(**values)
def opened(c=Config(),p=None):
    s=Paper('RAW',c);s.queue(p or plan(),bar(0));s.step(bar(1));return s

def mirrored(b):return Bar(b.index,b.time,b.time_close,200-b.open,200-b.low,200-b.high,200-b.close)

class ReferenceTests(unittest.TestCase):
    def test_signal_never_fills_same_bar(self):
        s=Paper('RAW',Config());s.queue(plan(),bar(0));s.step(bar(0));self.assertEqual(s.phase,1);self.assertEqual(s.ledger.entered,0)
    def test_next_open_effective_known_timestamps(self):
        s=opened();e=next(e for e in s.events if e['event']=='SIM_ENTRY');self.assertEqual(e['effective_time'],bar(1).time);self.assertEqual(e['known_time'],bar(1).time_close)
    def test_gap_rejection_keeps_last_closed(self):
        s=opened();s.step(bar(2,100,105,89,92));old=s.last_closed
        p=plan(pid='new',signal=2);s.queue(p,bar(2,100,105,89,92));s.step(bar(3,105,108,100,103));self.assertEqual(s.phase,0);self.assertEqual(s.ledger.cancelled,1);self.assertEqual(s.last_closed,old);self.assertEqual(s.last_cancelled['plan_id'],'new')
    def test_gap_through_initial_stop_never_entered(self):
        s=Paper('RAW',Config());s.queue(plan(),bar(0));s.step(bar(1,85,89,80,86));self.assertEqual(s.ledger.entered,0);self.assertEqual(s.ledger.cancelled,1)
    def test_gap_at_tp1_never_entered(self):
        s=Paper('RAW',Config(gap_filter=False));s.queue(plan(),bar(0));s.step(bar(1,110,112,109,111));self.assertEqual(s.ledger.cancelled,1)
    def test_stop_first_when_same_candle_has_tp(self):
        s=opened();s.step(bar(2,100,131,89,115));self.assertAlmostEqual(s.ledger.net,-1.03);self.assertEqual(s.ledger.ambiguous,1);self.assertFalse(any(e['event']=='SIM_TP1' for e in s.events))
    def test_stop_first_short_symmetric(self):
        s=Paper('RAW',Config());s.queue(plan(side=-1),mirrored(bar(0)));s.step(mirrored(bar(1)));s.step(mirrored(bar(2,100,131,89,115)));self.assertAlmostEqual(s.ledger.net,-1.03)
    def test_all_three_targets_once_and_cost_once(self):
        s=opened();s.step(bar(2,100,131,95,130));self.assertAlmostEqual(s.ledger.gross,2);self.assertAlmostEqual(s.ledger.costs,.03);self.assertAlmostEqual(s.ledger.net,1.97);self.assertAlmostEqual(sum(p['quantity_fraction'] for p in s.partials),1);s.step(bar(3,130,140,120,135));self.assertEqual(s.ledger.closed,1)
    def test_repeated_tp1_no_double_booking(self):
        s=opened();s.step(bar(2,100,111,99,109));s.step(bar(3,109,115,105,110));self.assertEqual(sum(e['event']=='SIM_TP1' for e in s.events),1);self.assertAlmostEqual(s.remaining,2/3)
    def test_lock_only_next_bar(self):
        s=opened();s.step(bar(2,100,112,95,110));self.assertEqual(s.stop,90);self.assertEqual(s.lock_bar,3);s.step(bar(3,110,112,101,105));self.assertAlmostEqual(s.stop,100.5)
    def test_lock_no_retroactive_same_candle_stop(self):
        s=opened();s.step(bar(2,100,111,95,109));self.assertEqual(s.phase,2);self.assertAlmostEqual(s.gross,1/3)
    def test_adverse_gap_exit_uses_open(self):
        s=opened();s.step(bar(2,80,85,75,81));self.assertAlmostEqual(s.ledger.gross,-2);self.assertEqual(s.last_closed['exit_price'],80);self.assertEqual(s.last_closed['exit_effective_time'],bar(2).time)
    def test_target_gap_conservative_limit_price(self):
        s=opened();s.step(bar(2,135,140,134,137));self.assertEqual([p['price'] for p in s.partials],[110,120,130])
    def test_timeout_scheduled_then_next_open(self):
        s=opened(Config(max_hold_bars=1));s.step(bar(2,101,104,98,103));self.assertTrue(s.exit_next_open);self.assertEqual(s.ledger.closed,0);s.step(bar(3,102,105,99,103));self.assertEqual(s.last_closed['exit_reason'],'TIME_NEXT_OPEN');self.assertAlmostEqual(s.ledger.gross,.2)
    def test_timeout_gap_stop_priority(self):
        s=opened(Config(max_hold_bars=1));s.step(bar(2));s.step(bar(3,85,88,80,82));self.assertEqual(s.last_closed['exit_reason'],'STOP_GAP');self.assertAlmostEqual(s.ledger.gross,-1.5)
    def test_targets_frozen_after_nonzero_open_deviation(self):
        s=Paper('RAW',Config());p=plan();s.queue(p,bar(0));s.step(bar(1,101,104,96,102));self.assertEqual(s.plan.targets,p.targets);self.assertEqual(s.risk,11)
    def test_risk_cost_rejected_on_fill(self):
        p=plan(cost_price=1.7);s=Paper('RAW',Config());s.queue(p,bar(0));s.step(bar(1,98,99,95,97));self.assertEqual(s.last_cancelled['reason'],'RISK_COST')
    def test_obstacle_room_rechecked_on_fill(self):
        p=plan(obstacle=110,required_room=1);s=Paper('RAW',Config());s.queue(p,bar(0));s.step(bar(1,101,104,98,102));self.assertEqual(s.last_cancelled['reason'],'ROOM')
    def test_news_at_known_open_blocks_pending(self):
        c=Config(news_windows=((bar(1).time,bar(1).time+60000),));s=Paper('RAW',c);s.queue(plan(),bar(0));s.step(bar(1));self.assertEqual(s.last_cancelled['reason'],'NEWS')
    def test_news_cannot_be_bypassed_by_gap_toggle(self):
        s=Paper('RAW',Config(gap_filter=False,manual_news_pause=True));s.queue(plan(),bar(0));s.step(bar(1));self.assertEqual(s.last_cancelled['reason'],'NEWS')
    def test_nonfinite_config_rejected(self):
        for values in ({'profit_lock_r':float('nan')},{'funding_long':-1},{'funding_short':float('inf')}):
            with self.assertRaises(ValueError):Config(**values)
    def test_financing_weights_partial_duration(self):
        s=opened(Config(funding_long=2));b=Bar(2,bar(1).time+86400000,bar(1).time+87300000,100,131,95,130);s.step(b);self.assertAlmostEqual(s.ledger.costs,(.3+2*87300000/86400000)/10)
    def test_day_reset_timezone_and_dst(self):
        from datetime import datetime
        def ms(x):return int(datetime.fromisoformat(x).timestamp()*1000)
        self.assertEqual(day_key(ms('2026-03-28T22:30:00+00:00'),'Europe/Sofia'),'2026-03-29')
        self.assertEqual(day_key(ms('2026-03-29T21:30:00+00:00'),'Europe/Sofia'),'2026-03-30')
    def test_day_bucket_uses_bar_open(self):
        s=opened();s.day='wrong';s.day_net=-3;s.day_streak=3;s.step(bar(2));self.assertEqual(s.day_net,0);self.assertEqual(s.day_streak,0)
    def test_zero_result_not_loss(self):
        l=Ledger();l.record(0,0,1);self.assertEqual((l.closed,l.zero,l.negative),(1,1,0))
    def test_outward_tick_rounding(self):
        self.assertAlmostEqual(stop_round(90.1236,1,.001),90.123);self.assertAlmostEqual(stop_round(109.8764,-1,.001),109.877);self.assertAlmostEqual(target_round(110.1231,1,.001),110.124)
    def test_nearest_obstacle_not_first_ema(self):
        self.assertEqual(nearest_obstacle([120,110,115],1,100),110);self.assertEqual(nearest_obstacle([80,90,85],-1,100),90)
    def test_invalid_na_zero_atr(self):
        for atr in (0,float('nan'),-1):
            with self.assertRaises(ValueError):plan(atr=atr)
    def test_invalid_ordered_targets(self):
        with self.assertRaises(ValueError):plan(targets=(110,110,130))
    def test_valid_plan_strict_max_risk(self):
        self.assertTrue(valid_plan(plan(),Config()));self.assertFalse(valid_plan(plan(max_risk_atr=.9),Config()))
    def test_model_selection_fixed_priority(self):
        p=pick([plan(3,pid='3'),plan(2,pid='2'),plan(1,pid='1')]);self.assertEqual(p.family,1)
    def test_conflict_no_portfolio_but_model_candidates_survive(self):
        e=Engine(Config());e.process(bar(0),[plan(1,pid='1'),plan(2,-1,pid='2')]);self.assertEqual(e.books['RAW'].phase,0);self.assertEqual(e.books['M1'].phase,1);self.assertEqual(e.books['M2'].phase,1)
    def test_independent_books_not_blocked_by_raw_capacity(self):
        e=Engine(Config());e.process(bar(0),[plan()]);e.process(bar(1),[plan(2,pid='fast',signal=1)]);self.assertEqual(e.books['M2'].phase,1);self.assertEqual(e.books['RAW'].phase,2);self.assertEqual(e.books['RAW'].ledger.capacity_skipped,1)
    def test_shadow_keeps_learning_when_filter_paused(self):
        e=Engine(Config(global_min_closed=1,global_stop_net_r=-1));e.books['RAW'].ledger.closed=1;e.books['RAW'].ledger.net=-2
        e.process(bar(0),[plan()]);self.assertEqual(e.books['RAW'].phase,1);self.assertEqual(e.books['M1'].phase,1);self.assertEqual(e.books['FILTERED'].phase,0)
    def test_gate_hysteresis_no_immediate_recovery(self):
        c=Config(global_min_closed=1,global_stop_net_r=-5,recovery_trades=5);l=Ledger(closed=30,net=-6);g=Gate();g.update(l,c,True);self.assertTrue(g.paused)
        l.net=1;l.closed=31;g.update(l,c,True);self.assertTrue(g.paused);l.closed=35;g.update(l,c,True);self.assertFalse(g.paused)
    def test_model_gate_and_positive_total(self):
        g=Gate();g.update(Ledger(closed=20,net=-4),Config(),False);self.assertTrue(g.paused)
    def test_small_sample_never_called_validated(self):
        e=Engine(Config());ps=e.process(bar(0),[plan()]);self.assertIn('UNVALIDATED',ps[0].evidence)
    def test_countertrend_observation_keeps_raw(self):
        e=Engine(Config());ps=e.process(bar(0),[plan(countertrend=True)]);self.assertFalse(ps[0].eligible);self.assertEqual(e.books['RAW'].phase,1)
    def test_filtered_limits_do_not_stop_model_discovery(self):
        e=Engine(Config());s=e.books['FILTERED'];s.day=day_key(bar(0).time,e.c.timezone);s.day_net=-3;e.process(bar(0),[plan()]);self.assertEqual(e.books['M1'].phase,1);self.assertEqual(s.phase,0)
    def test_state_cancel_then_no_same_bar_recreate(self):
        s=SetupLifecycle(1,1);self.assertTrue(s.start(1,10));s.cancel(2);self.assertFalse(s.start(2,20));self.assertTrue(s.start(3,30))
    def test_state_mss_retest_cannot_collapse_same_bar(self):
        s=SetupLifecycle(1,1);s.start(1,10);self.assertFalse(s.shift(1));self.assertTrue(s.shift(2));self.assertFalse(s.can_confirm(2,5));self.assertTrue(s.can_confirm(3,5));self.assertFalse(s.can_confirm(8,5))
    def test_state_ids_direction_and_model_isolated(self):
        a=SetupLifecycle(1,1);b=SetupLifecycle(3,1);c=SetupLifecycle(1,-1)
        for x in (a,b,c):x.start(0,100)
        self.assertEqual(len({a.id,b.id,c.id}),3);a.cancel(1);self.assertEqual(b.phase,1);self.assertEqual(c.phase,1)
    def test_alert_master_all_routes_off(self):
        for kind,book in [('WATCH','CORE'),('CONFIRMED','CORE'),('EVIDENCE','CORE'),('SIM_TP1','RAW'),('NATIVE_ENTRY','NATIVE')]:self.assertFalse(allowed_event(kind,book,master=False))
    def test_alert_categories_no_native_risk_bypass(self):
        self.assertFalse(allowed_event('NATIVE_DIVERGENCE','NATIVE',risk=False,paper=True));self.assertFalse(allowed_event('CONFIRMED','CORE',plan=False));self.assertFalse(allowed_event('WATCH','CORE',watch=False));self.assertFalse(allowed_event('SIM_SL','RAW',paper=False))
    def test_events_keep_all_tps(self):
        s=opened();s.step(bar(2,100,131,95,130));kinds=[e['event'] for e in s.events];self.assertTrue(all(k in kinds for k in ['SIM_TP1','SIM_TP2','SIM_TP3','CLOSED']));self.assertEqual(len({e['event_id'] for e in s.events}),len(s.events))
    def test_no_backdated_candidate(self):
        with self.assertRaises(ValueError):Paper('RAW',Config()).queue(plan(),bar(1))
    def test_replay_equals_incremental_same_inputs(self):
        bars=[bar(0),bar(1),bar(2,100,131,95,130)]
        import copy
        batch=Engine(Config())
        for b in bars:batch.process(b,[plan()] if b.index==0 else [])
        prefix=Engine(Config());prefix.process(bars[0],[plan()])
        resumed=copy.deepcopy(prefix)
        for b in bars[1:]:resumed.process(b,[])
        self.assertEqual(batch.books['RAW'].events,resumed.books['RAW'].events)
        self.assertEqual(batch.books['RAW'].trades,resumed.books['RAW'].trades)
    def test_future_bars_do_not_mutate_earlier_events(self):
        e=Engine(Config());e.process(bar(0),[plan()]);snapshot=[dict(x) for x in e.books['RAW'].events];e.process(bar(1),[]);self.assertEqual(e.books['RAW'].events[:len(snapshot)],snapshot)
    def test_overlap_input_rejected(self):
        e=Engine(Config());e.process(bar(0),[])
        with self.assertRaises(ValueError):e.process(bar(0),[])
    def test_long_short_mirror_full_campaign(self):
        a=opened();b=Paper('RAW',Config());b.queue(plan(side=-1),mirrored(bar(0)));b.step(mirrored(bar(1)))
        a.step(bar(2,100,131,95,130));b.step(mirrored(bar(2,100,131,95,130)));self.assertAlmostEqual(a.ledger.net,b.ledger.net)
    def test_open_plan_not_counted_as_closed(self):
        s=opened();self.assertEqual(s.ledger.entered,1);self.assertEqual(s.ledger.closed,0);self.assertEqual(s.ledger.net,0)

if __name__=='__main__':unittest.main()
