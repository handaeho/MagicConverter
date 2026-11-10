"""Direct rich readers/writers/HTML share facade worker limits and values."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from magic_converter.core.documents import rich_hwp as rich
from magic_converter.core.documents.hwp_documents import (
    DocumentData, Paragraph, TextRun, document_html, write_docx, write_hwpx)
from magic_converter.core.jobs import raw_api,job_control,specialized_worker
from magic_converter.core.jobs.job_control import Control,current,JobCancelled,JobTimedOut
from tests.documents.test_hwp_objects import binary_table_picture
from tests.jobs.test_parser_workers import alive


class RichRawApiTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.data=DocumentData([Paragraph([TextRun('Rich <text> & model',bold=True)])])
        self.docx=self.root/'normal.docx';self.hwpx=self.root/'normal.hwpx';self.hwp=self.root/'normal.hwp'
        write_docx(self.data,self.docx);write_hwpx(self.data,self.hwpx);binary_table_picture(self.hwp)

    def test_all_direct_rich_calls_use_owned_workers_and_keep_values(self):
        observed=[];real=specialized_worker.run_process
        def spy(command,**kwargs):
            request=json.loads(Path(command[-2]).read_text(encoding='utf-8'))
            observed.append((request['function'],request['deadline'],kwargs['memory_limit']))
            return real(command,**kwargs)
        with patch.object(specialized_worker,'run_process',side_effect=spy):
            for reader,path in [(rich.read_docx_rich,self.docx),(rich.read_hwpx_rich,self.hwpx)]:
                result=reader(path);self.assertIsInstance(result,DocumentData)
                self.assertIn('Rich <text> & model',result.text())
            self.assertIn('Cell',rich.read_hwp_rich(self.hwp).text())
            output=self.root/'exact.docx';self.assertIsNone(rich.write_docx_rich(self.data,output))
            self.assertTrue(output.is_file())
            html=rich.document_html_rich(self.data)
            self.assertIn('Rich &lt;text&gt; &amp; model',html)
            self.assertEqual(document_html(self.data),html)
        self.assertEqual(len(observed),6)
        self.assertEqual({name.rsplit(':',1)[-1] for name,_,_ in observed},
            {'read_docx_rich','read_hwpx_rich','read_hwp_rich','write_docx_rich','document_html_rich'})
        self.assertTrue(all(time.monotonic()<deadline<time.monotonic()+3601 for _,deadline,_ in observed))
        self.assertTrue(all(memory==2*1024**3 for _,_,memory in observed))
        self.assertIsNone(current.get())

    def test_existing_control_cancellation_and_transfer_budget_keep_output(self):
        output=self.root/'previous.docx';output.write_bytes(b'previous')
        control=Control(threading.Event(),lambda _:None,None);token=current.set(control)
        try:
            seen=[];real=specialized_worker.run_process
            def spy(command,**kw):
                seen.append(json.loads(Path(command[-2]).read_text(encoding='utf-8'))['deadline'])
                return real(command,**kw)
            with patch.object(specialized_worker,'run_process',side_effect=spy):
                rich.read_docx_rich(self.docx)
            self.assertEqual(seen,[None]);self.assertIs(current.get(),control)
            control.event.set()
            for call in (lambda:rich.read_docx_rich(self.docx),lambda:rich.write_docx_rich(self.data,output),lambda:document_html(self.data)):
                with self.assertRaises(JobCancelled):call()
        finally:current.reset(token)
        with patch.object(raw_api,'MAX_WIRE_BYTES',100),self.assertRaises(ValueError):
            rich.write_docx_rich(DocumentData([Paragraph([TextRun('x'*1000)])]),output)
        self.assertEqual(output.read_bytes(),b'previous')
        self.assertFalse(list(self.root.glob('.magic-raw-*')))

    def test_noncooperative_rich_reader_writer_html_stop_children_and_recover(self):
        output=self.root/'previous.docx'
        for call in (lambda:rich.read_docx_rich(self.docx),lambda:rich.write_docx_rich(self.data,output),lambda:document_html(self.data)):
            with self.subTest(call=call):
                ready=self.root/'ready.json';ready.unlink(missing_ok=True);output.write_bytes(b'previous')
                program='''
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
                with patch.object(job_control,'DEFAULT_API_TIMEOUT',4),patch.object(specialized_worker,'worker_command',
                        side_effect=lambda a,b:[sys.executable,'-c',program,str(a),str(b),str(ready)]):
                    with self.assertRaises(JobTimedOut):call()
                self.assertTrue(ready.is_file())
                self.assertTrue(all(not alive(pid) for pid in json.loads(ready.read_text(encoding='utf-8'))))
                self.assertEqual(output.read_bytes(),b'previous')
                self.assertIsNone(current.get());call()


if __name__=='__main__':unittest.main()
