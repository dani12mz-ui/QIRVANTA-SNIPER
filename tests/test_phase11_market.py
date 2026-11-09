from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from qirvanta_ultimate.market import MarketStore, MarketTick, SessionCalendar, TIMEFRAMES, poll_readonly
from qirvanta_ultimate.market_cache import archive_bars, cached_bars
from qirvanta_ultimate.mt5 import MT5Reader, MT5Unavailable

BASE = int(datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp() * 1000)
GOLD = 'TEST_XAU.s'
BTC = 'TEST_BTC_exact#'
SERVER = 'TEST_SERVER'
SOURCE = 'SYNTHETIC_FIXTURE'


def tick(at, price=100.0, *, symbol=GOLD, session_open=None, size=.01):
    return MarketTick(symbol, SERVER, price, price + .1, None, at, size, session_open)


class StoreTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = Path(scratch.name) / 'market.sqlite3'

    def store(self, **kwargs):
        return MarketStore(self.path, source=SOURCE, server=SERVER, symbols=(GOLD, BTC), **kwargs)

    def test_freshness_duplicate_conflict_gap_and_as_of_quote(self):
        with self.store() as store:
            first = store.ingest(tick(BASE), observed_ms=BASE + 50)
            second = store.ingest(tick(BASE + 100, 101.), observed_ms=BASE + 200)
            self.assertTrue(first.discontinuity)
            self.assertFalse(second.discontinuity)
            self.assertIsNone(store.quote_at(GOLD, BASE + 199))
            self.assertEqual(store.quote_at(GOLD, BASE + 200).bid, 101.)
            with self.store(read_only=True) as mirror:
                self.assertEqual(mirror.quote_at(GOLD, BASE + 250).bid, 101.)
                with self.assertRaisesRegex(ValueError, 'READ_ONLY'):
                    mirror.ingest(tick(BASE + 300), observed_ms=BASE + 300)
            dup = store.ingest(tick(BASE + 100, 101.), observed_ms=BASE + 300)
            self.assertEqual(dup.status, 'DUPLICATE')
            self.assertEqual(store.quote_at(GOLD, BASE + 301).bid, 101.)
            conflict = store.ingest(tick(BASE + 100, 102.), observed_ms=BASE + 400)
            self.assertEqual((conflict.status, conflict.reason), ('MISSING_DATA', 'CONFLICTING_PROVIDER_TIMESTAMP'))
            self.assertIsNone(store.quote_at(GOLD, BASE + 401))
            recovered = store.ingest(tick(BASE + 500), observed_ms=BASE + 500)
            self.assertTrue(recovered.discontinuity)
            self.assertIsNone(store.quote_at(GOLD, BASE + 501))
            delayed = store.ingest(tick(BASE + 900), observed_ms=BASE + 6_000)
            self.assertEqual(delayed.status, 'STALE_SOURCE')
            backwards = store.ingest(tick(BASE + 490), observed_ms=BASE + 6_001)
            self.assertEqual(backwards.status, 'STALE_SOURCE')
            future = store.ingest(tick(BASE + 6_003), observed_ms=BASE + 6_002)
            self.assertEqual(future.reason, 'FUTURE_PROVIDER_TIMESTAMP')
            self.assertEqual(future.symbol, GOLD)
            clean = store.ingest(tick(BASE + 6_004), observed_ms=BASE + 6_005)
            self.assertEqual(clean.reason, 'SOURCE_GAP')
            self.assertEqual(store.health(observed_ms=BASE + 6_006)['symbols'][GOLD]['last_fresh_quote']['bid'], 100.)
            health = store.health(observed_ms=BASE + 12_000)['symbols'][GOLD]
            self.assertEqual(health['status'], 'STALE_SOURCE')
            self.assertNotIn('last_fresh_quote', health)

    def test_all_intervals_confirm_only_when_causally_observed(self):
        end = BASE + 2 * 86_400_000
        with self.store(gap_ms=59_999) as store:
            for at in range(BASE, end + 1, 30_000):
                price = 102. if at == end - 30_000 else 100.
                update = store.ingest(tick(at, price), observed_ms=at + 333)
            self.assertEqual(update.closed_minutes, TIMEFRAMES)
            for minutes in TIMEFRAMES:
                self.assertEqual(store.closed_bars(GOLD, minutes, cutoff_ms=end), [])
                bars = store.closed_bars(GOLD, minutes, cutoff_ms=end + 333)
                self.assertEqual(bars[-1], {'time': end - minutes * 60_000, 'time_close': end,
                                            'open': 100., 'high': 102., 'low': 100., 'close': 102.})
            self.assertEqual(store.closed_bars(BTC, 1, cutoff_ms=end + 333), [])

    def test_restart_does_not_confirm_old_partial_or_interpolate_skipped_bars(self):
        with self.store(gap_ms=59_999) as store:
            for at in range(BASE, BASE + 210_000, 30_000):
                last = store.ingest(tick(at), observed_ms=at)
            self.assertEqual(len(store.closed_bars(GOLD, 1, cutoff_ms=BASE + 180_000)), 2)
        with self.store(gap_ms=59_999) as store:
            again = store.ingest(tick(BASE + 181_000), observed_ms=BASE + 181_000)
            self.assertGreater(again.sequence, last.sequence)
            self.assertEqual(again.reason, 'PROCESS_RESTART')
            jumped = store.ingest(tick(BASE + 480_000), observed_ms=BASE + 480_000)
            self.assertEqual(jumped.reason, 'SOURCE_GAP')
            self.assertEqual(store.closed_bars(GOLD, 1, cutoff_ms=BASE + 480_000), [])
            self.assertEqual(store.closed_bars(GOLD, 1, cutoff_ms=BASE + 180_000)[-1]['time_close'],
                             BASE + 180_000)
        with self.assertRaisesRegex(ValueError, 'STREAM_OR_POLICY_MISMATCH'):
            self.store()

    def test_session_calendar_uses_both_utc_clocks_and_coverage(self):
        calendar = SessionCalendar(SERVER, {GOLD: [(BASE + 100, BASE + 200)]},
                                   coverage_start_ms=BASE, coverage_end_ms=BASE + 1_000,
                                   evidence='SYNTHETIC_VERIFIED_UTC_WINDOW')
        self.assertIsNone(calendar.status(SERVER, BTC, BASE + 150, BASE + 160))
        self.assertIsNone(calendar.status(SERVER, GOLD, BASE + 150, BASE + 1_000))
        self.assertIs(calendar.status(SERVER, GOLD, BASE + 101, BASE + 199), True)
        self.assertIs(calendar.status(SERVER, GOLD, BASE + 101, BASE + 200), False)
        with self.store() as store:
            store.ingest(tick(BASE + 150, session_open=True), observed_ms=BASE + 150)
            ended = store.ingest(tick(BASE + 200, session_open=False), observed_ms=BASE + 200)
            self.assertTrue(ended.discontinuity)
            self.assertIsNone(store.quote_at(GOLD, BASE + 201))

    def test_features_recover_on_new_close_then_include_prior_observed_candles(self):
        with self.store(gap_ms=59_999) as store:
            for offset in range(0, 240_000, 30_000):
                store.ingest(tick(BASE + offset), observed_ms=BASE + offset)
            self.assertEqual(len(store.feature_bars(GOLD, 1, cutoff_ms=BASE + 210_000)), 2)
            store.mark_unavailable(GOLD, observed_ms=BASE + 211_000)
            self.assertEqual(store.feature_bars(GOLD, 1, cutoff_ms=BASE + 211_000), [])
            for offset in range(360_000, 480_001, 30_000):
                store.ingest(tick(BASE + offset), observed_ms=BASE + offset)
                if offset < 480_000:
                    self.assertEqual(store.feature_bars(GOLD, 1, cutoff_ms=BASE + offset), [])
            rows = store.feature_bars(GOLD, 1, cutoff_ms=BASE + 480_000)
            self.assertEqual([r['time'] - BASE for r in rows], [60_000, 120_000, 420_000])
            self.assertEqual(len(store.closed_bars(GOLD, 1, cutoff_ms=BASE + 480_000)), 1)
        with self.store(gap_ms=59_999, read_only=True) as reader:
            self.assertIsNone(reader.quote_at(GOLD, BASE + 480_000))
            self.assertEqual(reader.health(observed_ms=BASE + 480_000)['symbols'][GOLD]['reason'],
                             'MARKET_WRITER_STOPPED')

    def test_poll_reconnect_backoff_order_and_reader_shutdown(self):
        delays, life = [], []
        clock_values = iter(range(BASE + 20, BASE + 100))
        calendar = SessionCalendar(SERVER, {GOLD: [(BASE, BASE + 100)]},
                                   coverage_start_ms=BASE, coverage_end_ms=BASE + 100,
                                   evidence='SYNTHETIC_VERIFIED_UTC_WINDOW')

        class Reader:
            def symbol(self, name):
                return SimpleNamespace(symbol=name)

            def market_tick(self, name, *, contract):
                assert name == contract.symbol
                if name == BTC:
                    raise MT5Unavailable('MT5_DATA_UNAVAILABLE')
                return tick(BASE + 17 + len(life))

        @contextmanager
        def connector():
            life.append('ATTACH')
            if len(life) < 3:
                raise MT5Unavailable('MT5_ATTACH_FAILED')
            try:
                yield Reader()
            finally:
                life.append('SHUTDOWN')

        with self.store() as store:
            items = list(poll_readonly(connector, store, max_polls=4, clock=lambda: next(clock_values),
                                       sleeper=delays.append, session_calendar=calendar))
        self.assertEqual(delays, [1., 2., 1.])
        self.assertEqual(life, ['ATTACH'] * 3 + ['SHUTDOWN'])
        self.assertEqual([event.sequence for event in items], list(range(1, 9)))
        self.assertEqual([event.status for event in items],
                         ['MISSING_DATA'] * 4 + ['ACCEPTED', 'MISSING_DATA', 'DUPLICATE', 'MISSING_DATA'])
        self.assertTrue(items[4].tick.session_open)
        self.assertTrue(items[4].discontinuity)
        self.assertEqual(items[4].contract.symbol, GOLD)
        self.assertEqual(items[5].symbol, BTC)


class CacheAndReaderTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.cache = Path(scratch.name)

    def test_archive_revisions_provenance_immutability_and_causal_warmup(self):
        rows = [{'at_ms': BASE, 'open': 100., 'high': 101., 'low': 99., 'close': 101.,
                 'tick_volume': 5, 'reported_real_volume': 0, 'reported_spread_points': 3}]
        reader = SimpleNamespace(symbol=lambda name: SimpleNamespace(symbol=name, server=SERVER, chart_mode=0),
                                 bars=lambda *_: tuple(rows))
        window = BASE + 900_000
        session = SessionCalendar(SERVER, {GOLD: [(BASE, BASE + 300_000)]},
                                  coverage_start_ms=BASE, coverage_end_ms=window, evidence='SYNTHETIC')
        first = archive_bars(reader, self.cache, GOLD, 5, BASE, window,
                             source=SOURCE, retrieved_ms=lambda: window + 100, session_calendar=session)
        content = first.read_bytes()
        self.assertIn(b'VERIFIED_SESSION_GAP', content)
        self.assertIn(b'BROKER_NATIVE_HISTORY_RETRIEVED_AFTER_THE_FACT', content)
        lookup = {'source': SOURCE, 'server': SERVER, 'symbol': GOLD, 'minutes': 5}
        self.assertEqual(cached_bars(first, **lookup, cutoff_ms=window + 99), [])
        self.assertEqual(cached_bars(first, **lookup, cutoff_ms=window + 100, bid_only=True)[0]['close'], 101.)
        rows[0]['close'] = 100.5
        second = archive_bars(reader, self.cache, GOLD, 5, BASE, window,
                              source=SOURCE, retrieved_ms=window + 101, prior_manifest=first)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), content)
        self.assertIn(b'REVISED_HISTORY', second.read_bytes())
        with self.assertRaisesRegex(ValueError, 'IDENTITY_MISMATCH'):
            cached_bars(first, **(lookup | {'symbol': BTC}), cutoff_ms=window + 100)
        second.with_name('bars.json').write_bytes(b'[]')
        with self.assertRaisesRegex(ValueError, 'DATA_HASH_MISMATCH'):
            cached_bars(second, **lookup, cutoff_ms=window + 101)

    def test_reader_current_and_recent_tick_sources_are_distinct(self):
        from test_ultimate_risk_mt5 import FakeSDK

        sdk = FakeSDK()
        sdk.tick.time_msc = BASE
        sdk.tick.last = 0
        sdk.account.trade_allowed = False
        sdk.account.trade_expert = False
        sdk.COPY_TICKS_ALL = 123
        sdk.TIMEFRAME_M1 = 11
        sdk.TIMEFRAME_D1 = 99
        calls = []
        def history(name, at, count, kind):
            calls.append((name, at.tzinfo, count, kind))
            return [{'time_msc': BASE, 'bid': 2000., 'ask': 2000.2, 'last': 0., 'flags': 2}]
        sdk.copy_ticks_from = history
        reader = MT5Reader(sdk, sdk.account, lambda: BASE + 2_000)
        self.assertIs(reader.account().trade_allowed, False)
        self.assertIsNone(reader.market_tick('TEST_XAU').last)
        self.assertIsNone(reader.market_tick('TEST_XAU').session_open)
        self.assertEqual(reader.recent_ticks('TEST_XAU', BASE)[0]['status'], 'BACKFILLED')
        self.assertEqual(calls, [('TEST_XAU', timezone.utc, 1_000, 123)])
        for minutes in (1, 1440):
            self.assertEqual(reader.bars('TEST_XAU', minutes, BASE - 86_400_000, BASE), ())
        self.assertEqual([call[1] for call in sdk.calls if call[0] == 'history'], [11, 99])
        with self.assertRaisesRegex(MT5Unavailable, 'INVALID_MT5_TICK_HISTORY_WINDOW'):
            reader.recent_ticks('TEST_XAU', BASE, limit=100_000)
