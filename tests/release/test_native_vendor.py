import hashlib,io,json,tarfile,tempfile,unittest
from pathlib import Path
from tools.build.prepare_rhwp_rebuild import registry_vendor


class NativeVendorTests(unittest.TestCase):
    def archive(self,root,member_name='example-1.0.0/src/lib.rs',link=False):
        archive=root/'example-1.0.0.crate';data=b'pub fn normal() {}'
        with tarfile.open(archive,'w:gz')as bundle:
            member=tarfile.TarInfo(member_name)
            if link:member.type=tarfile.SYMTYPE;member.linkname='../../outside'
            else:member.size=len(data)
            bundle.addfile(member,None if link else io.BytesIO(data))
        return {'name':'example','version':'1.0.0','archive':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}

    def test_normal_directory_source_has_package_and_file_checksums(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);record=self.archive(root);out=root/'vendor'
            report=registry_vendor([record],root,out)
            checksum=json.loads((out/'example-1.0.0/.cargo-checksum.json').read_text(encoding='utf-8'))
            self.assertEqual(report['packages'],1);self.assertEqual(report['files'],1)
            self.assertEqual(checksum['package'],record['sha256'])
            self.assertEqual(checksum['files']['src/lib.rs'],hashlib.sha256(b'pub fn normal() {}').hexdigest())

    def test_traversal_symlink_and_changed_archive_are_rejected(self):
        for mode in('traversal','link','changed'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory()as folder:
                root=Path(folder);record=self.archive(root,'example-1.0.0/../../outside'if mode=='traversal'else'example-1.0.0/src/lib.rs',mode=='link')
                if mode=='changed':(root/record['archive']).write_bytes(b'altered')
                with self.assertRaises(ValueError):registry_vendor([record],root,root/'vendor')
                self.assertFalse((root/'outside').exists())

    def test_only_identical_case_alias_bytes_can_share_a_windows_file(self):
        for same in(True,False):
            with self.subTest(same=same),tempfile.TemporaryDirectory()as folder:
                root=Path(folder);archive=root/'example-1.0.0.crate'
                with tarfile.open(archive,'w:gz')as bundle:
                    for name,data in [('README.md',b'notice'),('Readme.md',b'notice'if same else b'different')]:
                        member=tarfile.TarInfo('example-1.0.0/'+name);member.size=len(data);bundle.addfile(member,io.BytesIO(data))
                record={'name':'example','version':'1.0.0','archive':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
                if same:
                    registry_vendor([record],root,root/'vendor')
                    checksum=json.loads((root/'vendor/example-1.0.0/.cargo-checksum.json').read_text(encoding='utf-8'))
                    self.assertEqual(checksum['files']['README.md'],checksum['files']['Readme.md'])
                else:
                    with self.assertRaisesRegex(ValueError,'bytes differ'):registry_vendor([record],root,root/'vendor')
