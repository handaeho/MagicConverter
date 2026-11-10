"""Reject unrelated/failed receipts instead of trusting a completion notice."""
import io
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from tools.validation.capture_zip_session import fixtures, gui_receipts


class ZipSessionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.expected = fixtures('ABCD')

    def entry(self, names, preset='zip-ABCD', identity='job'):
        files = []
        for name in names:
            target = self.output / (Path(name).stem + '.bmp')
            with Image.open(io.BytesIO(self.expected[name])) as original:
                original.save(target, 'BMP')
            files.append({'source': str(self.output / name), 'state': 'done', 'errors': [], 'warnings': [],
                          'outputs': [str(target)]})
        return {'id': identity, 'preset_id': preset, 'action_key': 'image:bmp', 'files': files}

    def test_normal_single_multiple_and_wrong_pixels(self):
        names = list(self.expected)
        entries = [self.entry(names[:1]), self.entry(names[1:], identity='pair')]
        checked = gui_receipts({'entries': entries}, self.expected, 'zip-ABCD', self.output)
        self.assertTrue(checked['single']['passed'])
        self.assertTrue(checked['multiple']['passed'])
        Image.new('RGB', (96, 64), 'black').save(entries[1]['files'][0]['outputs'][0], 'BMP')
        checked = gui_receipts({'entries': entries}, self.expected, 'zip-ABCD', self.output)
        self.assertTrue(checked['single']['passed'])
        self.assertFalse(checked['multiple']['passed'])

    def test_api_control_and_old_session_cannot_fill_manual_cases(self):
        entry = self.entry(list(self.expected)[:1], preset='api-ABCD')
        self.assertEqual(gui_receipts({'entries': [entry]}, self.expected, 'zip-ABCD', self.output), {})
        entry['preset_id'] = 'zip-ABCD'
        self.assertEqual(gui_receipts({'entries': [entry]}, fixtures('OLD1'), 'zip-ABCD', self.output), {})

    def test_latest_failed_attempt_and_wrong_output_directory_reject(self):
        entry = self.entry(list(self.expected)[:1])
        import copy
        failed = copy.deepcopy(entry); failed['id'] = 'failed'; failed['files'][0]['state'] = 'failed'
        checked = gui_receipts({'entries': [entry, failed]}, self.expected, 'zip-ABCD', self.output)
        self.assertFalse(checked['single']['passed'])
        checked = gui_receipts({'entries': [entry]}, self.expected, 'zip-ABCD', self.output / 'other-profile')
        self.assertFalse(checked['single']['passed'])


if __name__ == '__main__':
    unittest.main()
