"""Diagnostic updates do not reset a stalled activation's original budget."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from magic_converter.core.engines.office_com import ProcessLedger
from magic_converter.core.engines.office_process import run_helper
from magic_converter.core.jobs.job_control import JobTimedOut


class OfficeStageTests(unittest.TestCase):
    def test_details_keep_stage_stamp_and_only_new_stage_gets_new_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.processes.json'
            ledger = ProcessLedger(path)
            with patch('magic_converter.core.engines.office_com.time.monotonic_ns', side_effect=[100,200]):
                ledger.mark('starting', 'DispatchEx')
                ledger.mark('starting', 'server identity')
                self.assertEqual(json.loads(path.with_name('report.state.json').read_text())['stamp'], 100)
                ledger.mark('security')
                self.assertEqual(json.loads(path.with_name('report.state.json').read_text())['stamp'], 200)

    def test_stalled_stage_reports_detail_and_stops_owned_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.state.json'
            path.write_text(json.dumps({'stage':'starting','detail':'DispatchEx','stamp':1}))
            program = 'import time;time.sleep(30)'
            with patch('magic_converter.core.engines.office_process.STAGES', {'starting':(.3,'Activation')}):
                with self.assertRaisesRegex(JobTimedOut, 'DispatchEx'):
                    run_helper([sys.executable,'-c',program], path, timeout=5)
            path.unlink()  # No inherited reader remains.

    def test_workbook_export_uses_finite_request_budget_instead_of_open_budget(self):
        with tempfile.TemporaryDirectory()as folder:
            path=Path(folder)/'report.state.json'
            path.write_text(json.dumps({'stage':'office_export','detail':'export workbook PDF','stamp':1}))
            with patch('magic_converter.core.engines.office_process.STAGES',{'office_export':(.1,'Export')}):
                code,_=run_helper([sys.executable,'-c','import time;time.sleep(.4)'],path,timeout=2)
                self.assertEqual(code,0)
            with self.assertRaises(JobTimedOut):
                run_helper([sys.executable,'-c','import time;time.sleep(5)'],path,timeout=.3)
