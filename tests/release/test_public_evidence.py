"""A08 public evidence validation uses isolated synthetic, approved fixtures."""

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from magic_converter import __version__
from tools.build import build_inventory as inventory
from tools.build.release_evidence import SOURCE_LOCKS


def sha(data):
    return hashlib.sha256(data).hexdigest()


class PublicEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.packages = [{"SPDXID": "SPDXRef-project", "versionInfo": __version__},
                         {"SPDXID": "SPDXRef-fixture", "versionInfo": "1.2.3"}]
        self.source_files = {name: b"reviewed " + name.encode() for name in SOURCE_LOCKS}
        self.source_files.update({"LICENSE": b"Synthetic license text for tests.\n", "sources/fixture.c": b"int fixture(void) { return 42; }\n"})
        for name, data in self.source_files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.manifest = {
            "schema_version": 1, "project_version": __version__,
            "build_locks": {name: sha(self.source_files[name]) for name in SOURCE_LOCKS},
            "files": [{"path": name, "bytes": len(data), "sha256": sha(data)} for name, data in self.source_files.items()],
            "components": [{"id": "SPDXRef-project", "version": __version__, "files": list(SOURCE_LOCKS) + ["LICENSE"]},
                           {"id": "SPDXRef-fixture", "version": "1.2.3", "files": ["sources/fixture.c"]}],
        }
        self.policy = {"public_release_approved": True, "obligations_reviewed": True,
                       "project_license_file": "LICENSE", "corresponding_source_package": "source.zip",
                       "corresponding_source_manifest": "source-manifest.json"}
        self.write_fixture()

    def write_fixture(self):
        with zipfile.ZipFile(self.root / "source.zip", "w") as archive:
            for name, data in self.source_files.items():
                archive.writestr(name, data)
        (self.root / "source-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        for field, path in (("project_license", "LICENSE"), ("corresponding_source", "source.zip"), ("corresponding_source_manifest", "source-manifest.json")):
            self.policy[field + "_sha256"] = sha((self.root / path).read_bytes())
        self.write_policy()

    def write_policy(self):
        (self.root / "packaging/release-policy.json").write_text(json.dumps(self.policy), encoding="utf-8")

    def validate(self):
        with patch.object(inventory, "package_metadata", return_value=(self.packages, [], {})):
            return inventory.public_gate(self.root)

    def test_complete_hashed_versioned_source_fixture_passes(self):
        result = self.validate()
        self.assertEqual(result["components"], 2)
        self.assertEqual(result["project_version"], __version__)

    def test_empty_missing_corrupt_or_changed_evidence_is_rejected(self):
        for name in ("LICENSE", "source.zip", "source-manifest.json"):
            for data in (None, b"", b"not a valid archive or JSON\x00"):
                with self.subTest(file=name, data=data):
                    path = self.root / name
                    original = path.read_bytes()
                    if data is None:
                        path.unlink()
                    else:
                        path.write_bytes(data)
                    with self.assertRaises((ValueError, OSError, zipfile.BadZipFile)):
                        self.validate()
                    path.write_bytes(original)

    def test_whitespace_license_and_non_zip_with_updated_digests_are_rejected(self):
        for name, data, field in (("LICENSE", b" \n\t", "project_license_sha256"), ("source.zip", b"not ZIP", "corresponding_source_sha256")):
            with self.subTest(file=name):
                original = (self.root / name).read_bytes()
                previous = self.policy[field]
                (self.root / name).write_bytes(data)
                self.policy[field] = sha(data)
                self.write_policy()
                with self.assertRaises((ValueError, zipfile.BadZipFile)):
                    self.validate()
                (self.root / name).write_bytes(original)
                self.policy[field] = previous
                self.write_policy()

    def test_version_lock_component_file_and_checksum_mismatches_are_rejected(self):
        import copy
        original = copy.deepcopy(self.manifest)
        for change in (lambda m: m.update(project_version="0.0.0"),
                       lambda m: m["build_locks"].pop(SOURCE_LOCKS[0]),
                       lambda m: m["components"].pop(),
                       lambda m: m["components"][1].update(version="wrong"),
                       lambda m: m["components"][1].update(files=[]),
                       lambda m: m["files"].pop(),
                       lambda m: m["files"][0].update(sha256="0" * 64),
                       lambda m: m["files"].append(m["files"][0])):
            self.manifest = copy.deepcopy(original)
            change(self.manifest)
            self.write_fixture()
            with self.subTest(manifest=self.manifest), self.assertRaises(ValueError):
                self.validate()

    def test_unsafe_and_unlisted_archive_members_are_rejected_even_with_new_archive_hash(self):
        for name in ("../escape", "C:/escape", "unlisted.txt", "sources/FIXTURE.c"):
            self.write_fixture()
            with zipfile.ZipFile(self.root / "source.zip", "a") as archive:
                archive.writestr(name, b"unexpected")
            self.policy["corresponding_source_sha256"] = sha((self.root / "source.zip").read_bytes())
            self.write_policy()
            with self.subTest(member=name), self.assertRaises(ValueError):
                self.validate()

    def test_approval_flags_are_required_and_cannot_be_truthy_strings(self):
        for field in ("public_release_approved", "obligations_reviewed"):
            for value in (False, "true", 1):
                self.policy[field] = value
                self.write_policy()
                with self.assertRaisesRegex(ValueError, "policy unresolved"):
                    self.validate()
            self.policy[field] = True


if __name__ == "__main__":
    unittest.main()
