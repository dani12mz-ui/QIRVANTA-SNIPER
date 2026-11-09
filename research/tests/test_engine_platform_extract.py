import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from research.data.extract_platform import ROOT,extract_platform,inspect_platform
from diagnostics.protocol.extract_bars import parse_export,COLUMNS

SOURCE_SHA='a'*64


def export(minutes=5,alter=None):
    interval=minutes*60000;start=1704067200000
    rows=[[start+i*interval,start+(i+1)*interval,100,101,99,100,10,None,None,None,None,None,None] for i in range(3)]
    run={'schema':'qirvanta.native.v1','kind':'run','run_id':'TEST','symbol':'OANDA:XAUUSD','timeframe':str(minutes),'source_sha256':SOURCE_SHA,'bar_count':3,'first_bar_time':start,'last_bar_open':rows[-1][0],'cutoff':rows[-1][1]}
    snapshot={'schema':'qirvanta.native.v1','kind':'snapshot','run_id':'TEST','at_time':rows[-1][1],'at_bar':2}
    if alter:alter(rows,run,snapshot)
    body='QIRVANTA_DATA_BEGIN\n'+json.dumps(run)+'\n'+json.dumps(snapshot)+'\nQIRVANTA_DATA_END\nQIRVANTA_BARS_BEGIN\n'+json.dumps({'columns':COLUMNS})+'\n'+'\n'.join(json.dumps(r) for r in rows)+'\nQIRVANTA_BARS_END'
    out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(['Date','Message']);writer.writerow(['2026-11-09',body])
    return out.getvalue().encode()


class PlatformExtractTests(unittest.TestCase):
    def inspect(self,raw,minutes=5,source=SOURCE_SHA):
        return inspect_platform(parse_export(raw),expected_timeframe=minutes,expected_source_sha256=source)

    def test_actual_schema_all_supported_intervals(self):
        for minutes in (5,15,30):
            report=self.inspect(export(minutes),minutes)
            self.assertEqual(report['bar_count'],3)
            self.assertEqual(report['interval_ms'],minutes*60000)

    def test_wrong_source_hash_and_timeframe_rejected(self):
        with self.assertRaises(ValueError):self.inspect(export(),source='b'*64)
        with self.assertRaises(ValueError):self.inspect(export(),minutes=15)

    def test_bad_bar_duration_and_non_integer_json_token_rejected(self):
        for alter in (lambda rows,r,s:rows[1].__setitem__(1,rows[1][1]+1),lambda rows,r,s:rows[0].__setitem__(0,rows[0][0]+.5)):
            with self.assertRaises(ValueError):self.inspect(export(alter=alter))
        raw=export().replace(b'[1704067200000,',b'[1704067200000.00001,')
        with self.assertRaises(ValueError):self.inspect(raw)

    def test_duplicate_bounds_and_snapshot_mismatch_rejected(self):
        mutations=[lambda rows,r,s:rows.__setitem__(1,rows[0][:]),lambda rows,r,s:r.__setitem__('bar_count',4),lambda rows,r,s:s.__setitem__('at_time',s['at_time']+1),lambda rows,r,s:s.__setitem__('at_bar',3)]
        for alter in mutations:
            with self.assertRaises(ValueError):self.inspect(export(alter=alter))

    def test_truncated_and_duplicate_blocks_rejected(self):
        with self.assertRaises(ValueError):self.inspect(export().replace(b'QIRVANTA_BARS_END',b''))
        with self.assertRaises(ValueError):self.inspect(export().replace(b'QIRVANTA_BARS_END',b'QIRVANTA_BARS_END\nQIRVANTA_BARS_BEGIN'))

    def test_gaps_preserved_and_null_context_not_fabricated(self):
        def gap(rows,r,s):
            for row in rows[1:]:row[0]+=300000;row[1]+=300000
            r['last_bar_open']=rows[-1][0];r['cutoff']=rows[-1][1];s['at_time']=rows[-1][1]
        report=self.inspect(export(alter=gap))
        self.assertEqual(report['gap_count'],1)
        self.assertEqual(report['null_counts']['htfClose'],3)

    def test_local_archive_guard_no_overwrite_and_raw_hash_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'log.csv';source.write_bytes(export())
            target=Path(folder)/'dataset'
            with self.assertRaises(ValueError):extract_platform(source,target,expected_timeframe=5,expected_source_sha256=SOURCE_SHA,expected_log_sha256='bad')
            self.assertFalse(target.exists())
            manifest=extract_platform(source,target,expected_timeframe=5,expected_source_sha256=SOURCE_SHA)
            self.assertEqual(set(p.name for p in target.iterdir()),{'bars.csv','dataset_manifest.json'})
            self.assertIn('LOCAL_ONLY',manifest['redistribution'])
            with self.assertRaises(FileExistsError):extract_platform(source,target,expected_timeframe=5,expected_source_sha256=SOURCE_SHA)
            with self.assertRaises(ValueError):extract_platform(source,ROOT/'research/data/forbidden_market_archive',expected_timeframe=5,expected_source_sha256=SOURCE_SHA)


if __name__=='__main__':unittest.main()
