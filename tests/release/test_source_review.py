import unittest
from tools.build.prepare_source_review import selected


class SourceReviewSelectionTests(unittest.TestCase):
    def test_source_and_fixed_fixture_are_included(self):
        for name in ('magic_converter/core/pdf/pdf_layout.py','tools/ci/run_windows_validation.ps1',
                     'tests/fixtures/hwp/table-picture-spacing.hwpx','packaging/release-policy.json'):
            self.assertTrue(selected(name),name)

    def test_project_and_template_legal_files_survive_export(self):
        for name in ('LICENSE', 'NOTICE', 'magic_converter/data/hwpx/LICENSE',
                     'magic_converter/data/hwpx/NOTICE', 'licenses/runtime/package/COPYING',
                     'licenses/runtime/package/LICENSE.APACHE', 'assets/magic_converter-tool.svg'):
            self.assertTrue(selected(name), name)

    def test_native_binaries_credentials_and_user_documents_are_excluded(self):
        for name in ('vendor/other/native.dll','vendor/rhwp/rhwp.exe',
                     '.aws/credentials','.env','private-user-doc.hwpx','.git/config','dist/current/magic_converter.exe'):
            self.assertFalse(selected(name),name)


if __name__=='__main__':unittest.main()
