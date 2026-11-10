import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from tools.build.source_materials import verify_workspace, verify_crates


class SourceMaterialsTests(unittest.TestCase):
    def test_git_build_archive_accepts_original_and_rejects_changed_missing_extra_duplicate_traversal(self):
        data = {'Cargo.lock': b'lock', 'Cargo.toml': b'manifest', 'src/main.rs': b'fn main() {}'}
        expected = {n: {'mode': '100644', 'object_id': hashlib.sha1(b'blob ' + str(len(b)).encode() + b'\0' + b).hexdigest()}
                    for n, b in data.items()}
        variants = [list(data.items()), [('Cargo.lock', b'changed'), *list(data.items())[1:]],
                    list(data.items())[1:], [*data.items(), ('other', b'extra')],
                    [*data.items(), ('Cargo.lock', b'lock')], [*data.items(), ('../outside', b'bad')]]
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / 'source.zip'
            for index, entries in enumerate(variants):
                with self.subTest(index=index):
                    with zipfile.ZipFile(archive, 'w') as bundle:
                        for name, value in entries:
                            bundle.writestr(name, value)
                    if index == 0:
                        self.assertEqual(len(verify_workspace(archive, expected)[0]), 3)
                    else:
                        with self.assertRaises(ValueError):
                            verify_workspace(archive, expected)

    def test_registry_archive_checks_checksum_identity_missing_and_records_declared_terms(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'example-1.0.0.crate'
            def create(name='example'):
                content = f'[package]\nname="{name}"\nversion="1.0.0"\nlicense="MIT OR Apache-2.0"\n'.encode()
                with tarfile.open(archive, 'w:gz') as bundle:
                    member = tarfile.TarInfo('example-1.0.0/Cargo.toml'); member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
                return hashlib.sha256(archive.read_bytes()).hexdigest()
            def lock(digest):
                return ('[[package]]\nname="example"\nversion="1.0.0"\nsource="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="'+digest+'"\n').encode()
            original = create()
            self.assertEqual(verify_crates(lock(original), root)[0]['license_declared'], 'MIT OR Apache-2.0')
            archive.write_bytes(archive.read_bytes() + b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                verify_crates(lock(original), root)
            altered = create('different')
            with self.assertRaisesRegex(ValueError, 'identity'):
                verify_crates(lock(altered), root)
            archive.unlink()
            with self.assertRaisesRegex(ValueError, 'Missing'):
                verify_crates(lock(original), root)
