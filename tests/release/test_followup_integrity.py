"""Positive and hostile evidence at the frozen release boundary (A04-A06)."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.build import build_inventory as inventory
from tools.validation.verify_release import verify_sidecars


class FrozenIntegrityTests(unittest.TestCase):
    def test_original_native_bytes_pass_and_one_byte_changes_fail_for_every_owner(self):
        data = b"MZ reviewed native input\x00\x01"
        for owner in ("pymupdf", "pyside6-essentials", "python", "windows", "vendor-0"):
            entry = {"owner": owner, "group": "binary", "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()}
            with self.subTest(owner=owner):
                inventory.verify_frozen_input(entry, data, "native.dll")
                for altered in (data[:-1] + b"\x02", data + b"\x00", data[:-1]):
                    with self.assertRaisesRegex(ValueError, "Frozen input bytes differ"):
                        inventory.verify_frozen_input(entry, altered, "native.dll")

    def test_reviewed_package_claims_pass_and_semantic_tampering_is_rejected(self):
        packages, extracted, _ = inventory.package_metadata()
        original = {"packages": packages, "hasExtractedLicensingInfos": extracted}
        inventory.validate_package_metadata(original)
        cases = (
            ("SPDXRef-pymupdf", "versionInfo", "0.0.0-audit"),
            ("SPDXRef-pymupdf", "licenseDeclared", "MIT"),
            ("SPDXRef-pymupdf", "licenseConcluded", "MIT"),
            ("SPDXRef-pymupdf", "checksums", [{"algorithm": "SHA256", "checksumValue": "0" * 64}]),
            ("SPDXRef-pymupdf", "externalRefs", []),
            ("SPDXRef-vendor-0", "versionInfo", "wrong"),
            ("SPDXRef-vendor-0", "downloadLocation", "https://example.invalid/source"),
            ("SPDXRef-source-asset-0", "licenseDeclared", "MIT"),
            ("SPDXRef-python", "versionInfo", "0.0.0"),
            ("SPDXRef-project", "versionInfo", "0.0.0"),
        )
        for identity, field, value in cases:
            changed = copy.deepcopy(original)
            next(p for p in changed["packages"] if p["SPDXID"] == identity)[field] = value
            with self.subTest(identity=identity, field=field), self.assertRaisesRegex(ValueError, "package claims"):
                inventory.validate_package_metadata(changed)
        for changed in (copy.deepcopy(original), copy.deepcopy(original)):
            changed["packages"].append(copy.deepcopy(packages[0]))
            with self.assertRaises(ValueError):
                inventory.validate_package_metadata(changed)
        changed = copy.deepcopy(original)
        changed["hasExtractedLicensingInfos"][0]["extractedText"] = "wrong"
        with self.assertRaisesRegex(ValueError, "extracted licenses"):
            inventory.validate_package_metadata(changed)

    def test_release_sidecars_require_exact_embedded_bytes_and_both_files(self):
        class Archive:
            toc = {"build-metadata/" + n: None for n in ("sbom.spdx.json", "build-inputs.json")}

            def extract(self, name):
                return b'{"valid": true}\n'

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name in ("sbom.spdx.json", "build-inputs.json"):
                (directory / name).write_bytes(Archive().extract(name))
            self.assertEqual(len(verify_sidecars(directory, Archive())), 2)
            for name in ("sbom.spdx.json", "build-inputs.json"):
                for altered in (None, b"", b"not JSON", b'{"valid": false}', b'{"valid": true}'):
                    with self.subTest(file=name, altered=altered):
                        path = directory / name
                        if altered is None:
                            path.unlink()
                        else:
                            path.write_bytes(altered)
                        with self.assertRaises((ValueError, OSError)):
                            verify_sidecars(directory, Archive())
                        path.write_bytes(Archive().extract(name))


if __name__ == "__main__":
    unittest.main()
