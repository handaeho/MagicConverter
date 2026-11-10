"""Unbounded/reused COM interfaces refuse input; helpers are limited at start."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from contextlib import nullcontext
from magic_converter.core.engines.office_com import ProcessLedger, application


@unittest.skipUnless(os.name=='nt','Windows native budgets')
class OwnershipGateTests(unittest.TestCase):
    def test_unknown_server_never_yields_input_or_quits(self):
        for kind, collection in [('word','Documents'),('excel','Workbooks'),('powerpoint','Presentations')]:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                ledger=ProcessLedger(Path(directory)/'owned.processes.json')
                self.addCleanup(ledger.budget.close)
                app=MagicMock();getattr(app,collection).Count=0
                with patch('win32com.client.DispatchEx',return_value=app),patch.object(ledger,'track'),patch(
                        'magic_converter.core.engines.office_activation.bounded_application',return_value=nullcontext(app)):
                    with self.assertRaisesRegex(RuntimeError,'소유권|예산'),application(kind,ledger):
                        self.fail('Unbounded server yielded input')
                app.Quit.assert_not_called()

    def test_existing_document_server_is_refused_before_settings_or_input(self):
        for kind, collection in [('word','Documents'),('excel','Workbooks'),('powerpoint','Presentations')]:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory:
                ledger=ProcessLedger(Path(directory)/'owned.processes.json')
                self.addCleanup(ledger.budget.close)
                app=MagicMock(Visible=True);getattr(app,collection).Count=1
                app.DisplayAlerts=2;app.AutomationSecurity=1
                with patch('win32com.client.DispatchEx',return_value=app),patch(
                        'magic_converter.core.engines.office_activation.bounded_application',return_value=nullcontext(app)):
                    with self.assertRaisesRegex(RuntimeError,'기존'),application(kind,ledger):
                        self.fail('Existing user server yielded input')
                app.Quit.assert_not_called();getattr(app,collection).Open.assert_not_called()
                self.assertEqual(app.DisplayAlerts,2);self.assertEqual(app.AutomationSecurity,1)

    def test_helper_allocation_limit_is_set_before_running_code(self):
        from magic_converter.core.engines import office_process
        for size,expected in [(8*1024**2,0),(128*1024**2,42)]:
            with self.subTest(size=size),tempfile.TemporaryDirectory() as directory:
                code='import sys\ntry: b=bytearray('+str(size)+');sys.exit(0)\nexcept MemoryError:sys.exit(42)'
                with patch.object(office_process,'OFFICE_COMMIT_BYTES',96*1024**2):
                    status,errors=office_process.run_helper([sys.executable,'-c',code],Path(directory)/'state.json',timeout=10)
                self.assertEqual(status,expected,errors)


if __name__=='__main__':unittest.main()
