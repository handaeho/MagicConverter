"""Adversarial acquisition, provenance and SBOM gates (no network/native apps)."""

import base64
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build import build_inventory as inventory
from tools.dependencies import fetch_vendor as vendor
from tools.dependencies.collect_notices import license_file


class BuildGates(unittest.TestCase):
    def test_notice_collection_excludes_license_parser_code_and_cache(self):
        for path in (
            "packaging/licenses/__init__.py",
            "pip/_vendor/packaging/licenses/__pycache__/_spdx.cpython-314.pyc",
            "setuptools/_vendor/packaging/licenses/_spdx.py",
        ):
            self.assertFalse(license_file(path), path)
        for path in (
            "openpyxl-3.1.5.dist-info/LICENCE.rst",
            "et_xmlfile-2.0.0.dist-info/LICENCE.python",
            "pypdfium2_raw/licenses/BUILD_LICENSES/icu.txt",
        ):
            self.assertTrue(license_file(path), path)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def package(self, *, wrong_member=False):
        archive = self.cache / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("a", b"first")
            z.writestr("b", b"second")
            z.writestr("../../unselected", b"must never extract")
        entries = [
            {
                "member": n,
                "target": "vendor/fixture/" + n,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for n, data in (("a", b"first"), ("b", b"second"))
        ]
        if wrong_member:
            entries[-1]["sha256"] = "0" * 64
        artifact = {
            "filename": archive.name,
            "format": "zip",
            "sha256": vendor.sha(archive),
            "bytes": archive.stat().st_size,
            "url": "https://invalid.example/fixture.zip",
            "files": entries,
        }
        archive.rename(self.cache / (artifact["sha256"] + "-" + artifact["filename"]))
        return {"artifacts": [artifact]}

    def test_selected_members_only_and_offline_restore(self):
        manifest = self.package()
        vendor.acquire(self.root, self.cache, offline=True, manifest=manifest)
        self.assertEqual((self.root / "vendor/fixture/b").read_bytes(), b"second")
        self.assertFalse((self.root / "unselected").exists())
        (self.root / "vendor/fixture/a").unlink()
        vendor.acquire(self.root, self.cache, offline=True, manifest=manifest)
        self.assertEqual(vendor.verify(self.root, manifest), [])

    def test_archive_tamper_and_missing_cache_fail_closed(self):
        manifest = self.package()
        path = next(self.cache.glob("*.zip"))
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "Corrupt vendor cache"):
            vendor.acquire(self.root, self.cache, offline=True, manifest=manifest)
        self.assertFalse((self.root / "vendor/fixture/a").exists())
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            vendor.acquire(self.root, self.cache, offline=True, manifest=manifest)

    def test_late_member_failure_does_not_publish_first_file(self):
        with self.assertRaisesRegex(ValueError, "member SHA256"):
            vendor.acquire(
                self.root,
                self.cache,
                offline=True,
                manifest=self.package(wrong_member=True),
            )
        self.assertFalse((self.root / "vendor/fixture/a").exists())

    def test_changed_existing_file_needs_explicit_repair(self):
        manifest = self.package()
        path = self.root / "vendor/fixture/a"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "review before --repair"):
            vendor.acquire(self.root, self.cache, offline=True, manifest=manifest)
        self.assertEqual(path.read_bytes(), b"changed")
        vendor.acquire(
            self.root, self.cache, offline=True, repair=True, manifest=manifest
        )
        self.assertEqual(path.read_bytes(), b"first")

    def test_manifest_target_escape_is_rejected(self):
        manifest = self.package()
        manifest["artifacts"][0]["files"][0]["target"] = "vendor/../../escape"
        with self.assertRaisesRegex(ValueError, "escapes"):
            vendor.acquire(self.root, self.cache, offline=True, manifest=manifest)

    def test_unlocked_and_changed_wheel_sources_are_rejected(self):
        path = self.root / ".venv/Lib/site-packages/unreviewed.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"print('modified')")
        with self.assertRaisesRegex(ValueError, "Unowned/unlocked"):
            inventory.source_owner(path, self.root, {})
        expected = (
            "sha256="
            + base64.urlsafe_b64encode(hashlib.sha256(b"original").digest())
            .rstrip(b"=")
            .decode()
        )
        with self.assertRaisesRegex(ValueError, "differs from locked wheel"):
            inventory.source_owner(
                path, self.root, {path: [("fixture", "module.py", expected)]}
            )

    def test_changed_notice_and_path_escape_are_rejected(self):
        path = self.root / "LICENSE"
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed reviewed input"):
            inventory.checked(
                self.root, "LICENSE", hashlib.sha256(b"original").hexdigest()
            )
        with self.assertRaisesRegex(ValueError, "escapes checkout"):
            inventory.checked(self.root, "../outside", "0" * 64)

    def test_incomplete_spdx_and_public_policy_are_rejected(self):
        with self.assertRaises(Exception):
            inventory.validate_sbom({"spdxVersion": "SPDX-2.3"})
        (self.root / "packaging").mkdir()
        (self.root / "packaging/release-policy.json").write_text(
            json.dumps(
                {
                    "public_release_approved": False,
                    "obligations_reviewed": False,
                    "project_license_file": None,
                    "corresponding_source_package": None,
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "policy unresolved"):
            inventory.public_gate(self.root)


if __name__ == "__main__":
    unittest.main()
