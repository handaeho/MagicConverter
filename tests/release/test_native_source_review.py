import unittest
from tools.build.review_native_sources import notice_comparison,configuration_flags

class NativeSourceReviewTests(unittest.TestCase):
    def test_source_notice_line_endings_are_classified_without_changing_raw_hashes(self):
        result=notice_comparison(b'Copyright A\nMIT\n',b'Copyright A\r\nMIT\r\n')
        self.assertEqual(result['difference'],'line_endings_only')
        self.assertNotEqual(result['source_sha256'],result['deployed_sha256'])
        self.assertEqual(notice_comparison(b'MIT\n',b'MIT\n')['difference'],'identical')

    def test_changed_copyright_or_license_is_not_an_eol_difference(self):
        self.assertEqual(notice_comparison(b'Copyright A\nMIT\n',b'Copyright B\nMIT\n')['difference'],'content_differs')
        self.assertEqual(notice_comparison(b'MIT\n',b'GPL\n')['difference'],'content_differs')

    def test_real_configure_flags_and_empty_or_nonfree_controls(self):
        result=configuration_flags('configuration:\n --enable-gpl\n --enable-version3\n --enable-libx264\n')
        self.assertTrue(result['gpl_enabled']);self.assertTrue(result['version3_enabled'])
        self.assertFalse(result['nonfree_enabled']);self.assertEqual(result['external_lib_options'],['--enable-libx264'])
        self.assertTrue(configuration_flags('--enable-nonfree')['nonfree_enabled'])
        self.assertFalse(configuration_flags('--enable-static')['gpl_enabled'])
        with self.assertRaises(ValueError):configuration_flags('')

if __name__=='__main__':unittest.main()
