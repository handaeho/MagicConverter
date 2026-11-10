"""Registered direct converters keep finite defaults and atomic failures."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_execution import execute_file
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.jobs import job_control, specialized_worker
from magic_converter.core.jobs.job_control import Control,current,JobTimedOut
from tests.jobs.test_parser_workers import alive


class RegisteredApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.csv=self.root/'rows.csv';self.csv.write_text('key,value\nnormal,42\n',encoding='utf-8')
        self.image=self.root/'image.png';Image.new('RGB',(32,24),'red').save(self.image)

    def call(self,source,key):
        request=prepare_request([source],key,create_catalog())
        return execute_file(request,request.files[0],None,lambda _:None,lambda _:None)

    def test_data_and_library_direct_calls_default_to_finite_owned_workers(self):
        real=specialized_worker.run_process;observed=[]
        def spy(command,**kwargs):
            request=json.loads(Path(command[-2]).read_text())
            observed.append((request['deadline'],kwargs['memory_limit']))
            return real(command,**kwargs)
        with patch.object(specialized_worker,'run_process',side_effect=spy):
            data=self.call(self.csv,'data:json');image=self.call(self.image,'image:jpg')
        self.assertEqual(json.loads(data.read_text(encoding='utf-8-sig')),[{'key':'normal','value':'42'}])
        with Image.open(image) as picture:self.assertEqual(picture.size,(32,24))
        self.assertEqual(len(observed),2)
        self.assertTrue(all(time.monotonic()<d<time.monotonic()+3601 and m==2*1024**3 for d,m in observed))
        self.assertIsNone(current.get())

    def test_explicit_unlimited_control_and_invalid_input_keep_contract(self):
        control=Control(threading.Event(),lambda _:None,None);token=current.set(control)
        real=specialized_worker.run_process;observed=[]
        def spy(command,**kwargs):
            self.assertIs(current.get(),control)
            observed.append(json.loads(Path(command[-2]).read_text())['deadline'])
            return real(command,**kwargs)
        try:
            with patch.object(specialized_worker,'run_process',side_effect=spy):self.call(self.csv,'data:json')
            self.assertEqual(observed,[None]);self.assertIs(current.get(),control)
            bad=self.root/'bad.json';bad.write_text('{invalid')
            with self.assertRaises(Exception):self.call(bad,'data:yaml')
            self.assertFalse(list(self.root.glob('bad*.yaml')))
        finally:current.reset(token)

    def test_default_deadline_stops_noncooperative_data_and_image_children_then_recovers(self):
        for source,key in ((self.csv,'data:json'),(self.image,'image:jpg')):
            with self.subTest(key=key):
                ready=self.root/'ready.json';ready.unlink(missing_ok=True)
                program='''
import sys,time,subprocess,os,json
from pathlib import Path
from magic_converter.core.jobs.specialized_worker import helper_main
from magic_converter.core.specialized import data_converter
from magic_converter.core.conversion import converter
def blocked(*a,**kw):
 child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
 Path(sys.argv[3]).write_text(json.dumps([os.getpid(),child.pid]))
 time.sleep(30)
data_converter.convert=blocked
converter.convert_image=blocked
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''
                with patch.object(job_control,'DEFAULT_API_TIMEOUT',3),patch.object(specialized_worker,'worker_command',
                    side_effect=lambda a,b:[sys.executable,'-c',program,str(a),str(b),str(ready)]):
                    with self.assertRaises(JobTimedOut):self.call(source,key)
                self.assertIsNone(current.get());self.assertTrue(ready.is_file())
                self.assertTrue(all(not alive(pid) for pid in json.loads(ready.read_text())))
                self.assertFalse(list(self.root.glob('*converted*')))
                output=self.call(source,key);self.assertTrue(output.is_file());output.unlink()


if __name__=='__main__':unittest.main()
