import copy,hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from tools.build.native_build_receipts import review_build

COMMIT='1'*40;H=lambda b:hashlib.sha256(b).hexdigest();LICENSE=b'Fixed MIT notice'


class NativeBuildReceiptTests(unittest.TestCase):
    def fixtures(self,folder):
        root=Path(folder);exe=b'rebuilt test bytes';digest=H(exe);stock=H(b'unchanged shipped bytes')
        toolchain={'rust_version':'1.93.1','manifest_sha256':'a'*64}
        build={'build_executed':True,'build_succeeded':True,'source_commit':COMMIT,'rust_version':'1.93.1','toolchain_manifest_sha256':'a'*64,
            'executable_sha256':digest,'legal_approval':False,'complete_corresponding_source':False,'source_preservation':{'status':'passed','git_files':1726,'registry_files':18234},
            'resolved_features':{'rhwp':['default']},'commands':[{'phase':'native-cargo-metadata','command':['cargo.exe','metadata','--offline','--locked'],'status':'passed','exit':0},
             {'phase':'native-cargo-build','command':['cargo.exe','build','--offline','--locked','--release','--bin','rhwp','--target','x86_64-pc-windows-msvc'],'status':'passed','exit':0}]}
        functional={'status':'qualified_functional_correspondence_recorded','source_commit':COMMIT,'rebuilt_sha256':digest,'shipped_sha256':stock,
            'byte_identical_to_shipped':False,'legal_approval':False,'complete_corresponding_source':False,'cases':[
                {'case':name,'status':'passed_functional','max_mae':0,'geometry_equal':True,'strict_visual_status':'matched',
                 'outputs':{key:{'pages':pages}for key in('rebuilt','shipped')}}for name,pages in(('paragraph',1),('roman',1),('actual33',33))]}
        return root,exe,stock,toolchain,build,functional

    def check(self,folder,build,functional,exe,stock,toolchain,license=LICENSE):
        receipt=folder/'build.json';archive=folder/'review.zip';receipt.write_text(json.dumps(build),encoding='utf-8')
        with zipfile.ZipFile(archive,'w')as z:
            z.writestr('rhwp.exe',exe);z.writestr('review.json',json.dumps(functional));z.writestr('LICENSE',license)
        return review_build(receipt,archive,COMMIT,stock,toolchain,H(LICENSE))

    def test_real_payload_hash_and_nonempty_functional_receipt_are_accepted_without_approval(self):
        with tempfile.TemporaryDirectory()as tmp:
            root,exe,stock,t,b,f=self.fixtures(tmp);r=self.check(root,b,f,exe,stock,t)
            self.assertEqual(r['functional_pages'],35);self.assertTrue(r['strict_functional_pixel_match'])
            self.assertFalse(r['byte_identical']);self.assertFalse(r['legal_approval'])

    def test_tampered_binary_or_license_and_empty_functional_evidence_are_rejected(self):
        for kind in('binary','license','empty','duplicate'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as tmp:
                root,exe,stock,t,b,f=self.fixtures(tmp)
                if kind=='binary':exe+=b'x'
                if kind=='empty':f['cases']=[]
                if kind=='duplicate':f['cases'][1]=copy.deepcopy(f['cases'][0])
                with self.assertRaises(ValueError):self.check(root,b,f,exe,stock,t,b'wrong'if kind=='license'else LICENSE)

    def test_failed_build_or_misbound_recipe_or_missing_source_features_are_rejected(self):
        for kind in('failed','recipe','features','source','rights','empty_counts','boolean_exit','boolean_mae'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory()as tmp:
                root,exe,stock,t,b,f=self.fixtures(tmp)
                if kind=='failed':b['commands'][1]['exit']=1
                if kind=='recipe':b['commands'][1]['command']+=['--bin','other']
                if kind=='features':b['resolved_features']={}
                if kind=='source':b.pop('source_preservation')
                if kind=='rights':b['legal_approval']=True
                if kind=='empty_counts':b['source_preservation']={'status':'passed'}
                if kind=='boolean_exit':b['commands'][1]['exit']=False
                if kind=='boolean_mae':f['cases'][0]['max_mae']=False
                with self.assertRaises(ValueError):self.check(root,b,f,exe,stock,t)

    def test_profile_environment_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory()as tmp:
            root,exe,stock,t,b,f=self.fixtures(tmp);b['profile_experiment']={'overrides':{'lto':False,'codegen_units':16}}
            b['environment']={'CARGO_PROFILE_RELEASE_LTO':'true','CARGO_PROFILE_RELEASE_CODEGEN_UNITS':'16'}
            with self.assertRaises(ValueError):self.check(root,b,f,exe,stock,t)

    def test_functional_pixel_difference_is_recorded_separately_from_build_success(self):
        with tempfile.TemporaryDirectory()as tmp:
            root,exe,stock,t,b,f=self.fixtures(tmp);f['cases'][0].update(max_mae=.01,strict_visual_status='different')
            self.assertFalse(self.check(root,b,f,exe,stock,t)['strict_functional_pixel_match'])
