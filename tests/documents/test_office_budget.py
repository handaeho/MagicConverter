"""Real OS allocation, identity exclusion and helper-crash cleanup controls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

from magic_converter.core.engines.office_budget import OfficeProcessBudget
from magic_converter.core.engines.office_com import ProcessLedger


@unittest.skipUnless(os.name == 'nt', 'Windows Job allocation limits')
class OfficeBudgetTests(unittest.TestCase):
    def child(self, root, size):
        ready = root / 'allocate'
        code = ("import sys,time;from pathlib import Path; p=Path(sys.argv[1]);"
                "\nwhile not p.exists():time.sleep(.01)"
                "\ntry: b=bytearray(int(sys.argv[2]));sys.exit(0)"
                "\nexcept MemoryError:sys.exit(42)")
        process = subprocess.Popen([sys.executable, '-c', code, str(ready), str(size)],
            creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        import win32process
        created = str(win32process.GetProcessTimes(process._handle)['CreationTime'])
        return process, created, ready

    def test_external_normal_allocation_and_excess_rejection(self):
        for size, expected in [(8 * 1024**2, 0), (128 * 1024**2, 42)]:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as folder:
                process, created, ready = self.child(Path(folder), size)
                budget = OfficeProcessBudget([], memory_limit=96 * 1024**2)
                try:
                    self.assertTrue(budget.attach(process.pid, created))
                    self.assertTrue(budget.attach(process.pid, created))
                    self.assertEqual(len(budget.identities), 1)
                    ready.touch()
                    self.assertEqual(process.wait(timeout=10), expected)
                finally:
                    budget.close()

    def test_preexisting_and_wrong_creation_identity_are_never_attached_or_killed(self):
        with tempfile.TemporaryDirectory() as folder:
            process, created, ready = self.child(Path(folder), 8 * 1024**2)
            for before, identity in [([process.pid], created), ([], 'wrong-time')]:
                budget = OfficeProcessBudget(before)
                self.assertFalse(budget.attach(process.pid, identity))
                self.assertIsNone(budget.job)
                budget.close()
                self.assertIsNone(process.poll())
            ready.touch()
            self.assertEqual(process.wait(timeout=10), 0)

    def test_closing_the_helper_owned_job_stops_only_attached_native_server(self):
        with tempfile.TemporaryDirectory() as folder:
            process, created, _ = self.child(Path(folder), 0)
            budget = OfficeProcessBudget([])
            self.assertTrue(budget.attach(process.pid, created))
            self.assertIsNone(process.poll())
            budget.close()
            process.wait(timeout=5)  # KILL_ON_JOB_CLOSE may use exit code zero.
            self.assertFalse((Path(folder) / 'allocate').exists())

    def test_successful_exit_finishes_naturally_while_job_is_still_active(self):
        with tempfile.TemporaryDirectory() as folder:
            process, created, ready = self.child(Path(folder), 8 * 1024**2)
            budget = OfficeProcessBudget([])
            try:
                self.assertTrue(budget.attach(process.pid, created))
                ready.touch()
                result = budget.wait_for_exit([(process.pid, created)])
                self.assertEqual(result['processes'][0]['status'], 'exited')
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertIsNotNone(budget.job)
                self.assertLess(result['seconds'], 1.25)
            finally:
                budget.close()

    def test_grace_timeout_keeps_job_limit_and_then_kills_only_owned_child(self):
        with tempfile.TemporaryDirectory() as folder:
            process, created, _ = self.child(Path(folder), 0)
            budget = OfficeProcessBudget([])
            try:
                self.assertTrue(budget.attach(process.pid, created))
                result = budget.wait_for_exit([(process.pid, created)], seconds=.05)
                self.assertEqual(result['processes'][0]['status'], 'timeout')
                self.assertLess(result['seconds'], .5)
                self.assertIsNone(process.poll())
                self.assertIsNotNone(budget.job)
            finally:
                budget.close()
            process.wait(timeout=5)

    def test_grace_does_not_open_unattached_or_wrong_creation_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            process, created, ready = self.child(Path(folder), 0)
            budget = OfficeProcessBudget([])
            try:
                with patch('win32api.OpenProcess') as lookup:
                    result = budget.wait_for_exit([(process.pid, created)])
                    lookup.assert_not_called()
                self.assertEqual(result['processes'][0]['status'], 'not_attached')
                self.assertTrue(budget.attach(process.pid, created))
                with patch('win32process.GetProcessTimes', return_value={'CreationTime': 'reused'}), patch(
                        'win32event.WaitForSingleObject') as wait:
                    result = budget.wait_for_exit([(process.pid, created)])
                    wait.assert_not_called()
                self.assertEqual(result['processes'][0]['status'], 'identity_changed')
                ready.touch()
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                budget.close()

    def test_grace_accepts_only_bounded_finite_deadlines(self):
        for seconds in (-1, 1.01, float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                OfficeProcessBudget([]).wait_for_exit([], seconds)

    def test_abrupt_helper_exit_also_closes_the_external_server_job(self):
        with tempfile.TemporaryDirectory() as folder:
            process, created, _ = self.child(Path(folder), 0)
            program = ("import os,sys;from magic_converter.core.engines.office_budget import OfficeProcessBudget;"
                       "b=OfficeProcessBudget([]);assert b.attach(int(sys.argv[1]),sys.argv[2]);os._exit(0)")
            done = subprocess.run([sys.executable, '-c', program, str(process.pid), created], timeout=10,
                                  capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(done.returncode, 0, done.stderr)
            process.wait(timeout=5)

    def test_positive_ownership_budget_failure_is_not_suppressed(self):
        import win32process
        with tempfile.TemporaryDirectory() as folder:
            process, created, _ = self.child(Path(folder), 0)
            ledger = ProcessLedger(Path(folder) / 'report.processes.json')
            ledger.before.discard(process.pid)
            app = MagicMock(Hwnd=123)
            with patch('win32process.GetWindowThreadProcessId', return_value=(1, process.pid)), patch.object(
                    ledger.budget, 'attach', side_effect=OSError('assignment refused')):
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, 'assignment refused'):
                        ledger.track(app, 'excel')
            owned = json.loads(ledger.path.read_text())['owned']
            self.assertEqual([(p['pid'], p['created']) for p in owned], [(process.pid, created)])

    def test_scm_receipt_identifies_post_identity_limit_without_claiming_prelaunch(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ProcessLedger(Path(folder)/'report.processes.json')
            process, created, _ = self.child(Path(folder), 0)
            app = MagicMock(Hwnd=123)
            try:
                with patch('win32process.GetWindowThreadProcessId', return_value=(1, process.pid)):
                    ledger.track(app, 'excel')
                receipt = json.loads(ledger.path.read_text(encoding='utf-8'))['owned'][0]
                self.assertEqual((receipt['pid'], receipt['created']), (process.pid, created))
                self.assertEqual(receipt['activation'], 'scm')
                self.assertEqual(receipt['budget_phase'], 'after_native_identity')
                self.assertIs(receipt['preactivation_bounded'], False)
                self.assertEqual(receipt['commit_limit_bytes'], 2*1024**3)
            finally:
                ledger.budget.close()

    def test_reused_application_is_excluded_even_when_its_pid_was_not_in_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = ProcessLedger(Path(folder) / 'report.processes.json')
            self.addCleanup(ledger.budget.close)
            process, created, ready = self.child(Path(folder), 8 * 1024**2)
            self.assertNotIn(process.pid, ledger.before)
            self.assertNotIn(process.pid, ledger.budget.before)
            app = MagicMock(Hwnd=123)
            ledger.exclude(app)
            with patch('win32process.GetWindowThreadProcessId', return_value=(1, process.pid)) as lookup:
                ledger.track(app, 'excel')
                lookup.assert_not_called()
            self.assertEqual(ledger.owned, [])
            ledger.budget.close()
            self.assertIsNone(process.poll())
            import gc, weakref
            reference = weakref.ref(app)
            object_id = id(app)
            del app
            gc.collect()
            self.assertIsNone(reference())
            self.assertNotIn(object_id, ledger.unowned_apps)
            ready.touch()
            self.assertEqual(process.wait(timeout=10), 0)
