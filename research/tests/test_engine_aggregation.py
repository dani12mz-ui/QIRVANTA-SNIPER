import unittest
from research.data.aggregate import aggregate_m15_to_m30


def source():
    rows=[(100,103,99,101,5),(101,104,98,102,6),(102,103,100,101,7),(101,102,99,100,8)]
    return [dict(zip(('open','high','low','close','volume'),r),time=1704067200000+i*900000,time_close=1704067200000+(i+1)*900000) for i,r in enumerate(rows)]


class AggregationTests(unittest.TestCase):
    def test_exact_ohlcv_and_source_pair_mapping(self):
        result,manifest=aggregate_m15_to_m30(source())
        self.assertEqual(result[0],{'time':1704067200000,'time_close':1704069000000,'open':100,'high':104,'low':98,'close':102,'volume':11})
        self.assertEqual(manifest['source_index_pairs'],[[0,1],[2,3]])
        self.assertEqual(manifest['dropped_source_bars'],0)

    def test_incomplete_last_bar_remains_unpublished_until_closed_pair(self):
        data=source()
        short,manifest=aggregate_m15_to_m30(data[:3])
        full,_=aggregate_m15_to_m30(data)
        self.assertEqual(short,full[:1])
        self.assertEqual(manifest['dropped_source_bars'],1)
        self.assertEqual(manifest['dropped_partial_bins'][0]['source_indices'],[2])

    def test_missing_half_is_reported_never_joined_across_gap(self):
        data=source();del data[1]
        result,manifest=aggregate_m15_to_m30(data)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['time'],1704069000000)
        self.assertEqual(manifest['dropped_source_bars'],1)

    def test_partial_initial_bin_dropped(self):
        result,manifest=aggregate_m15_to_m30(source()[1:])
        self.assertEqual(len(result),1)
        self.assertEqual(manifest['dropped_source_bars'],1)

    def test_unknown_volume_does_not_become_zero_or_true_vwap(self):
        data=source();data[0]['volume']=None
        result,manifest=aggregate_m15_to_m30(data)
        self.assertIsNone(result[0]['volume'])
        self.assertIn('UNVERIFIED',manifest['volume_status'])
        self.assertNotIn('bar_vwap',result[0])

    def test_wrong_timeframe_rejected_not_downsampled(self):
        data=source()
        for i,b in enumerate(data):b.update(time=1704067200000+i*300000,time_close=1704067200000+(i+1)*300000)
        with self.assertRaises(ValueError):aggregate_m15_to_m30(data)


if __name__=='__main__':unittest.main()
