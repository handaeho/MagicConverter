"""Public PDF merge/ZIP calls must retain owned worker supervision."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

import pymupdf as fitz
from magic_converter.core.conversion.converter import create_zip
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.jobs import combined_worker, specialized_worker
from magic_converter.core.jobs.job_control import Control,current,JobCancelled,JobTimedOut
from magic_converter.core.pdf.pdf_tools import merge_pdf
from magic_converter.core.settings.preferences import OutputRule
from tests.jobs.test_parser_workers import alive


class DirectCombinedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.paths=[]
        for name in ('One','Two'):
            path=self.root/(name+'.pdf')
            with fitz.open() as d:d.new_page().insert_text((20,30),name);d.save(path)
            self.paths.append(path)

    def call(self,key):
        rule=OutputRule('folder',str(self.root/'output'),'{stem}_{preset}_{tag}')
        if key=='zip':return create_zip(tuple(self.paths),rule=rule,preset_name='Direct')
        request=prepare_request(self.paths[::-1],key,create_catalog(),output=rule)
        return merge_pdf(request,lambda _:None)

    def test_direct_apis_use_finite_default_owned_boundary_and_keep_output_contract(self):
        original=specialized_worker.run_process;calls=[]
        def spy(command,**kwargs):
            request=json.loads(Path(command[-2]).read_text())
            calls.append((request['deadline'],kwargs['memory_limit']))
            return original(command,**kwargs)
        with patch.object(specialized_worker,'run_process',side_effect=spy):
            zip_path=self.call('zip');pdf_path=self.call('tool:pdf_merge')
        self.assertEqual(zip_path.name,'One_Direct_converted.zip')
        with zipfile.ZipFile(zip_path) as z:
            self.assertEqual([z.read(n) for n in z.namelist()],[p.read_bytes() for p in self.paths])
        with fitz.open(pdf_path) as d:self.assertEqual([p.get_text().strip() for p in d],['Two','One'])
        self.assertEqual(len(calls),2)
        self.assertTrue(all(time.monotonic()<deadline<time.monotonic()+3601 and memory==2*1024**3 for deadline,memory in calls))
        self.assertIsNone(current.get())

    def test_explicit_no_deadline_and_outer_control_identity_are_preserved(self):
        control=Control(threading.Event(),lambda _:None,None);token=current.set(control)
        original=specialized_worker.run_process;deadlines=[]
        def spy(command,**kwargs):
            self.assertIs(current.get(),control)
            deadlines.append(json.loads(Path(command[-2]).read_text())['deadline'])
            return original(command,**kwargs)
        try:
            with patch.object(specialized_worker,'run_process',side_effect=spy):self.call('zip')
            self.assertIs(current.get(),control);self.assertEqual(deadlines,[None])
        finally:current.reset(token)

    def test_direct_cancel_deadline_kill_noncooperative_encoder_and_descendant_without_publication(self):
        for key in ('zip','tool:pdf_merge'):
            for deadline in (False,True):
                with self.subTest(key=key,deadline=deadline):
                    ready=self.root/'ready.json';ready.unlink(missing_ok=True)
                    program='''
import json,os,subprocess,sys,time
from pathlib import Path
from magic_converter.core.jobs.specialized_worker import helper_main
from magic_converter.core.conversion import converter
from magic_converter.core.pdf import pdf_tools
def blocked(*args,**kwargs):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    tmp=Path(sys.argv[3]+'.tmp');tmp.write_text(json.dumps({'worker':os.getpid(),'child':child.pid}));tmp.replace(sys.argv[3])
    time.sleep(30)
converter.create_zip=blocked
pdf_tools.merge_pdf=blocked
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''
                    control=Control(threading.Event(),lambda _:None);token=current.set(control)
                    def interrupt():
                        end=time.monotonic()+15
                        while not ready.exists() and time.monotonic()<end:time.sleep(.01)
                        if deadline:control.deadline=time.monotonic()+.05
                        else:control.event.set()
                    thread=threading.Thread(target=interrupt);thread.start()
                    try:
                        command=lambda a,b:[sys.executable,'-c',program,str(a),str(b),str(ready)]
                        with patch.object(specialized_worker,'worker_command',side_effect=command),self.assertRaises(JobTimedOut if deadline else JobCancelled):self.call(key)
                    finally:thread.join(16);current.reset(token)
                    self.assertTrue(ready.exists())
                    self.assertTrue(all(not alive(pid) for pid in json.loads(ready.read_text()).values()))
                    output=self.root/'output'
                    self.assertFalse(output.exists() and any(output.iterdir()))
        self.assertTrue(self.call('zip').is_file())


if __name__=='__main__':unittest.main()
