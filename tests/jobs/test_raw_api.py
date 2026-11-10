"""Raw facades preserve values and bound non-cooperative parser processes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image
from magic_converter.core.conversion.converter import convert_image, ConversionError
from magic_converter.core.documents.hwp_documents import (
    DocumentData, Paragraph, TextRun, Picture, Equation, Table, Cell,
    read_docx, read_hwpx, write_docx, write_hwpx,
)
from magic_converter.core.jobs import raw_api, job_control, specialized_worker
from magic_converter.core.jobs.job_control import Control, current, JobCancelled, JobTimedOut
from magic_converter.core.specialized.data_converter import read_data
from tests.jobs.test_parser_workers import alive


class RawApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv = self.root / 'rows.csv'
        self.csv.write_text('a,b\nnormal,42\n', encoding='utf-8')
        self.image = self.root / 'input.png'
        with Image.new('RGB', (32, 24), 'red') as picture:
            picture.save(self.image)

    def test_normal_values_notices_default_deadline_and_explicit_control(self):
        notices, observed = [], []
        real = specialized_worker.run_process
        def spy(command, **kwargs):
            observed.append((json.loads(Path(command[-2]).read_text())['deadline'], kwargs['memory_limit']))
            return real(command, **kwargs)
        with patch.object(specialized_worker, 'run_process', side_effect=spy):
            self.assertEqual(read_data(self.csv, 'csv', notices.append), [{'a':'normal','b':'42'}])
            self.assertTrue(notices)
            control = Control(threading.Event(), lambda _: None, None)
            token = current.set(control)
            try:
                read_data(self.csv, 'csv')
                self.assertIs(current.get(), control)
            finally:
                current.reset(token)
        self.assertTrue(time.monotonic() < observed[0][0] < time.monotonic()+3601)
        self.assertIsNone(observed[1][0])
        self.assertTrue(all(x[1] == 2*1024**3 for x in observed))
        self.assertIsNone(current.get())

    def test_data_errors_keep_types_and_document_locations(self):
        path = self.root / 'bad.json'
        path.write_bytes(b'{\n invalid}')
        with self.assertRaises(json.JSONDecodeError) as error:
            read_data(path, 'json')
        self.assertEqual((error.exception.doc, error.exception.lineno), ('{\n invalid}', 2))
        path.write_text('{"a":1,"a":2}', encoding='utf-8')
        with self.assertRaises(ConversionError):
            read_data(path, 'json')

    def test_model_roundtrip_preserves_typed_fields_and_image_bytes(self):
        stream = io.BytesIO()
        with Image.new('RGB', (10, 12), 'red') as picture:
            picture.save(stream, 'PNG')
        data = DocumentData([Paragraph([TextRun('Raw model'), Equation('x')]),
            Table(1, 1, [Cell(0, 0, [Paragraph([Picture(stream.getvalue(), 10, 12)])])])])
        self.assertEqual(raw_api.decode(raw_api.encode(data)), data)
        for writer, reader, suffix in ((write_docx, read_docx, '.docx'), (write_hwpx, read_hwpx, '.hwpx')):
            destination = self.root / ('model' + suffix)
            self.assertIsNone(writer(data, destination))
            result = reader(destination)
            self.assertIsInstance(result, DocumentData)
            self.assertIn('Raw model', result.text())
            self.assertEqual(result.blocks[1].cells[0].paragraphs[0].runs[0].data, stream.getvalue())

    def test_atomic_exact_destination_failure_cancel_and_normal_overwrite(self):
        destination = self.root / 'output.jpg'
        destination.write_bytes(b'existing output')
        bad = self.root / 'bad.png'; bad.write_bytes(b'not an image')
        with self.assertRaises(Exception):
            convert_image(bad, destination, '.jpg')
        self.assertEqual(destination.read_bytes(), b'existing output')
        event = threading.Event(); event.set()
        token = current.set(Control(event, lambda _: None))
        try:
            with self.assertRaises(JobCancelled):
                convert_image(self.image, destination, '.jpg')
        finally:
            current.reset(token)
        self.assertEqual(destination.read_bytes(), b'existing output')
        self.assertIsNone(convert_image(self.image, destination, '.jpg'))
        with Image.open(destination) as picture:
            self.assertEqual(picture.size, (32, 24))
        self.assertFalse(list(self.root.glob('.magic-raw-*')))

    def test_raw_svg_text_has_headless_font_database_and_invalid_svg_keeps_output(self):
        source = self.root / 'text.svg'
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">'
            '<text x="10" y="40" font-size="24">Normal SVG text</text></svg>', encoding='utf-8')
        output = self.root / 'text.png'
        convert_image(source, output, '.png')
        with Image.open(output) as picture:
            self.assertEqual(picture.size, (200,80))
            self.assertIsNotNone(picture.getchannel('A').getbbox())
        previous = output.read_bytes()
        source.write_text('<svg broken')
        with self.assertRaises(ConversionError):convert_image(source, output, '.png')
        self.assertEqual(output.read_bytes(), previous)

    def test_transfer_rejects_unknown_tags_and_budget_but_keeps_ordinary_dict_keys(self):
        value = {'DocumentData': ['bytes', 'not a tag'], 'value': None}
        self.assertEqual(raw_api.decode(raw_api.encode(value)), value)
        with self.assertRaises(ValueError):
            raw_api.decode(['unknown', {}])
        with patch.object(raw_api, 'MAX_NODES', 5), self.assertRaises(ValueError):
            raw_api.encode(list(range(10)))
        with patch.object(raw_api, 'MAX_WIRE_BYTES', 100), self.assertRaises(ValueError):
            raw_api.encode(b'x'*100)

    def test_worker_refuses_forged_writer_contract_and_nonprivate_destination(self):
        source = self.root / 'arguments.json'
        outside = self.root / 'preserved.hwpx'; outside.write_bytes(b'previous')
        action = 'magic_converter.core.documents.hwp_documents:write_hwpx'
        for flag, values in ((False, {'data':DocumentData()}),
                             (True, {'data':DocumentData(), 'destination':outside})):
            raw_api.write_wire(source, values)
            request = {'source':str(source), 'function':action, 'file_result':flag}
            with self.assertRaisesRegex(ValueError, 'output contract'):
                raw_api.run_request(request, self.root/'result.hwpx', lambda _: None)
            self.assertEqual(outside.read_bytes(), b'previous')
            self.assertFalse((self.root/'result.hwpx').exists())

    def test_default_deadline_stops_child_tree_and_recovers_every_raw_family(self):
        output = self.root / 'result.png'
        calls = (lambda: read_data(self.csv, 'csv'),
                 lambda: convert_image(self.image, output, '.png'),
                 lambda: write_hwpx(DocumentData([Paragraph([TextRun('Recovery')])]), output))
        for call in calls:
            ready = self.root / 'ready.json'; ready.unlink(missing_ok=True)
            output.write_bytes(b'previous')
            program = '''
import sys,time,subprocess,os,json
from pathlib import Path
from magic_converter.core.jobs import raw_api
from magic_converter.core.jobs.specialized_worker import helper_main
def blocked(*a,**kw):
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
 Path(sys.argv[3]).write_text(json.dumps([os.getpid(),child.pid]))
 time.sleep(30)
raw_api.run_request=blocked
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''
            with patch.object(job_control, 'DEFAULT_API_TIMEOUT', 4), patch.object(specialized_worker, 'worker_command',
                    side_effect=lambda a,b: [sys.executable,'-c',program,str(a),str(b),str(ready)]):
                with self.assertRaises(JobTimedOut):
                    call()
            self.assertTrue(ready.is_file())
            self.assertTrue(all(not alive(pid) for pid in json.loads(ready.read_text())))
            self.assertEqual(output.read_bytes(), b'previous')
            self.assertIsNone(current.get())
            call()


if __name__ == '__main__':
    unittest.main()
