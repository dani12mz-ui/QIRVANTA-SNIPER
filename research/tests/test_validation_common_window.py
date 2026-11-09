import unittest
from research.validation.common_window import interval_from_availability


class CommonWindowTests(unittest.TestCase):
    def features(self, times, h4=True):
        return [{'available_at':t,'ready':True,'h1':{'bias':0},'h4':{'bias':0 if h4 else None}} for t in times]

    def test_latest_warmup_and_earliest_complete_end(self):
        bars={'5':[{'time_close':150},{'time_close':400}], '30':[{'time_close':160},{'time_close':390}]}
        f={'5':self.features([150,400]), '30':self.features([160,390])}
        r=interval_from_availability(f,bars,alignment_ms=100)
        self.assertEqual((r['start'],r['end']),(200,300))
        self.assertFalse(r['uses_returns'])

    def test_neutral_bias_is_available_not_missing(self):
        bars={'15':[{'time_close':100},{'time_close':500}]}
        self.assertEqual(interval_from_availability({'15':self.features([100,500])},bars,100)['status'],'AVAILABLE')

    def test_missing_h4_never_manufactures_context(self):
        bars={'5':[{'time_close':100},{'time_close':500}]}
        self.assertEqual(interval_from_availability({'5':self.features([100,500],False)},bars,100)['status'],'BLOCKED_INSUFFICIENT_CLOSED_HTF_WARMUP')

    def test_no_post_warmup_interval_blocks(self):
        bars={'30':[{'time_close':150}]}
        self.assertEqual(interval_from_availability({'30':self.features([150])},bars,100)['status'],'BLOCKED_NO_SHARED_POST_WARMUP_INTERVAL')


if __name__=='__main__':unittest.main()
