"""Successful rhwp output can still report real font/layout limitations."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from magic_converter.core.engines import rhwp_engine as rhwp
from magic_converter.core.jobs.process_runner import ProcessResult
from magic_converter.core.conversion.converter import ConversionError


class RhwpDiagnosticsTests(unittest.TestCase):
    def test_normal_messages_are_not_warnings(self):
        self.assertEqual(rhwp.diagnostics('Document loaded\nExport complete\n'), [])

    def test_warning_overflow_deduplication_and_bounds(self):
        warnings = rhwp.diagnostics('WARN: missing font\nWARN: missing font\n'
                                    'LAYOUT_OVERFLOW: page=31\nLAYOUT_OVERFLOW_DRAW: para=472\n')
        self.assertEqual(len(warnings), 3)
        self.assertIn('page=31', warnings[1])
        self.assertLessEqual(len(rhwp.diagnostics('\n'.join('WARN: '+str(i)+'x'*1000 for i in range(50)))), 11)

    def test_valid_output_forwards_diagnostics_and_invalid_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'input.hwpx'; source.write_bytes(b'source')
            destination=source.with_suffix('.pdf')
            notices=[]
            with patch.object(rhwp,'locate',return_value=Path('rhwp.exe')), \
                    patch.object(rhwp,'run_process',return_value=ProcessResult(0,'complete','WARN: missing font')), \
                    patch('magic_converter.core.engines.hwp_converter.validate_output') as validate:
                rhwp.convert(source,destination,warning=notices.append)
                validate.assert_called_once_with(destination)
                self.assertIn('missing font',notices[0])
            notices.clear()
            with patch.object(rhwp,'locate',return_value=Path('rhwp.exe')), \
                    patch.object(rhwp,'run_process',return_value=ProcessResult(0,'','WARN: missing font')), \
                    patch('magic_converter.core.engines.hwp_converter.validate_output',side_effect=ConversionError('invalid')):
                with self.assertRaises(ConversionError): rhwp.convert(source,destination,warning=notices.append)
            self.assertEqual(notices,[])


if __name__ == '__main__': unittest.main()
