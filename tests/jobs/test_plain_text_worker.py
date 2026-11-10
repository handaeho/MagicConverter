import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.conversion_execution import execute_file


class PlainTextWorkerTests(unittest.TestCase):
    def test_bom_encodings_newlines_and_literals_keep_existing_output_and_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = '한글 <b>& literal\r\n# heading\n\n끝 😀'
            for encoding in ('utf-8-sig', 'utf-16', 'cp949'):
                with self.subTest(encoding=encoding):
                    text = value if encoding != 'cp949' else value.replace('😀', '문자')
                    source = root / (encoding + '.txt')
                    raw = text.encode(encoding); source.write_bytes(raw)
                    request = prepare_request([source], 'document:md', create_catalog())
                    result = execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
                    expected = root / 'expected.md'; expected.write_text(text, encoding='utf-8')
                    self.assertEqual(result.read_bytes(), expected.read_bytes())
                    self.assertEqual(source.read_bytes(), raw)

    def test_real_worker_skips_gui_and_rich_engines_for_plain_text(self):
        script = r'''
import json,sys,tempfile
from pathlib import Path
from dataclasses import asdict
from magic_converter.core.jobs.library_worker import run_request
from magic_converter.core.conversion.conversion_catalog import ConversionEdge
from magic_converter.core.settings.preferences import EncoderOptions,ToolOptions
with tempfile.TemporaryDirectory() as folder:
 root=Path(folder); source=root/'source.txt'; source.write_text('plain 한글',encoding='utf-8')
 request={'source':str(source),'edge':asdict(ConversionEdge('txt','md','documents','document:md')),
          'options':asdict(EncoderOptions()),'tools':asdict(ToolOptions()),'run_id':'test'}
 target=root/'result.md';run_request(request,target,lambda _:None,lambda _:None,lambda _:None)
 assert target.read_text(encoding='utf-8')=='plain 한글'
 assert not any(n.startswith(('PySide6','pymupdf','docx','openpyxl')) for n in sys.modules)
 print(json.dumps({'status':'passed'}))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
