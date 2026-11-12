import base64
import csv
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from tools.build.windows_runtime_bundle import clean_pymupdf_wheel, verify_pymupdf_build_metadata


class PyMuPDFWheelTests(unittest.TestCase):
    def test_removes_build_metadata_and_updates_record_without_touching_native_files(self):
        metadata_name = "pymupdf/_build.py"
        record_name = "pymupdf-1.28.2.dist-info/RECORD"
        binary_name = "pymupdf/_mupdf.pyd"
        license_name = "pymupdf-1.28.2.dist-info/licenses/COPYING"
        metadata = (
            "pymupdf_git_branch = 'local'\n"
            "pymupdf_git_comment = 'commit summary'\n"
            "pymupdf_git_diff = ('working-tree\\n'\n"
            "                    'diff')\n"
            "pymupdf_git_diff_n = len(pymupdf_git_diff)\n"
            "pymupdf_git_sha = 'local-commit'\n"
            "mupdf_location = 'build-machine-path'\n"
            "pymupdf_version = '1.28.2'\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.whl"
            destination = Path(directory) / "clean.whl"
            with ZipFile(source, "w") as wheel:
                wheel.writestr(metadata_name, metadata)
                wheel.writestr(record_name, f"{metadata_name},old-hash,0\n{record_name},,\n")
                wheel.writestr(binary_name, b"native code")
                wheel.writestr(license_name, b"license")

            with self.assertRaisesRegex(ValueError, "contains local build metadata"):
                verify_pymupdf_build_metadata(source)
            clean_pymupdf_wheel(source, destination)
            verify_pymupdf_build_metadata(destination)

            with ZipFile(destination) as wheel:
                cleaned = wheel.read(metadata_name)
                self.assertIn(b"pymupdf_version = '1.28.2'", cleaned)
                self.assertNotIn(b"working-tree", cleaned)
                self.assertEqual(wheel.read(binary_name), b"native code")
                self.assertEqual(wheel.read(license_name), b"license")
                rows = list(csv.reader(io.StringIO(wheel.read(record_name).decode("utf-8"))))
                digest = base64.urlsafe_b64encode(hashlib.sha256(cleaned).digest()).rstrip(b"=").decode("ascii")
                self.assertEqual(rows[0], [metadata_name, "sha256=" + digest, str(len(cleaned))])
