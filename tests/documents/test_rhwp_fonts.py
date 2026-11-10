"""Use installed Korean monospace coverage, never a missing default or Latin-only face."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile
from magic_converter.core.engines.rhwp_engine import fallback_mono


class RhwpFontTests(unittest.TestCase):
    def test_shared_all_faces_are_not_hidden_by_the_hwp_only_directory(self):
        from magic_converter.core.engines.rhwp_engine import font_directories
        with tempfile.TemporaryDirectory()as d:
            pass
            private=shared/'Hwp';common=shared/'All'
            for p in(system,private,common):p.mkdir(parents=True)
            pass

    def test_font_paths_include_private_and_user_faces_without_activation(self):
        from magic_converter.core.engines.rhwp_engine import font_directories
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); system = root/'Windows/Fonts'; user = root/'User/Microsoft/Windows/Fonts'
            pass
            for p in (system, user, private): p.mkdir(parents=True)
            pass
            pass
            pass

    def test_pdf_command_passes_each_font_path_and_non_pdf_does_not_discover(self):
        from magic_converter.core.engines import rhwp_engine as engine
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); folders=[root/'system',root/'private']; source=root/'source.hwpx'
            with patch.object(engine,'locate',return_value=root/'rhwp.exe'), \
                 patch.object(engine,'font_directories',return_value=folders) as discovery, \
                 patch.object(engine,'fallback_mono',return_value=None), \
                 patch.object(engine,'run_process',return_value=SimpleNamespace(returncode=0,stderr='')) as run, \
                 patch('magic_converter.core.engines.hwp_converter.validate_output'):
                engine._convert(source,root/'output.pdf',lambda _:None,timeout=5)
                command=run.call_args.args[0]
                self.assertEqual([command[i+1]for i,v in enumerate(command)if v=='--font-path'],list(map(str,folders)))
                discovery.reset_mock();engine._convert(source,root/'output.hwp',lambda _:None,timeout=5)
                discovery.assert_not_called()

    def test_available_default_and_missing_default_with_covered_fixed_width_fallback(self):
        from magic_converter.core.pdf import pdf_fonts
        full=SimpleNamespace(has_glyph=lambda _:True,glyph_advance=lambda _: .5)
        for missing in (False,True):
            def resolve(name):return None if missing and name=='D2Coding' else SimpleNamespace(family=name)
            with patch.object(pdf_fonts,'resolve_font',side_effect=resolve),patch.object(pdf_fonts,'font_metrics',return_value=full):
                self.assertEqual(fallback_mono().family,'GulimChe' if missing else 'D2Coding')

    def test_latin_only_proportional_or_missing_faces_do_not_hide_engine_warning(self):
        from magic_converter.core.pdf import pdf_fonts
        for metric in (SimpleNamespace(has_glyph=lambda n:n<128,glyph_advance=lambda _: .5),
                       SimpleNamespace(has_glyph=lambda _:True,glyph_advance=lambda n:.2 if n==ord('i') else .8)):
            with patch.object(pdf_fonts,'resolve_font',return_value=object()),patch.object(pdf_fonts,'font_metrics',return_value=metric):
                self.assertIsNone(fallback_mono())
        with patch.object(pdf_fonts,'resolve_font',return_value=None):self.assertIsNone(fallback_mono())


if __name__=='__main__':unittest.main()
