"""Page anchor equality does not hide physical font/position differences."""
import unittest
from unittest.mock import patch
import pymupdf as fitz
from magic_converter.core.diagnostics.text_geometry import page_text_geometry, locate_anchor, compare_anchor_geometry, _text_paint_states


class TextGeometryTests(unittest.TestCase):
    def test_zero_alpha_and_ignore_search_text_do_not_claim_a_painted_face_change(self):
        for options in ({'fill_opacity':0}, {'render_mode':3}):
            with self.subTest(options=options),fitz.open() as reference,fitz.open() as candidate:
                reference.new_page().insert_text((72,100),'Dot',fontname='helv')
                candidate.new_page().insert_text((72,100),'Dot',fontname='tiro',**options)
                result=compare_anchor_geometry(reference,candidate,[{'paragraph':0,'reference_page':1,'candidate_page':1,'anchor':'Dot'}])
                self.assertEqual(result['encoding_normalized_font_name_changed_characters'],3)
                self.assertEqual(result['positive_opacity_font_name_changed_characters'],0)
                self.assertEqual(result['nonpainting_font_name_changed_characters'],3)
                self.assertEqual(result['unknown_paint_state_paired_characters'],0)

    def test_positive_opacity_font_change_remains_reported(self):
        with fitz.open() as reference,fitz.open() as candidate:
            reference.new_page().insert_text((72,100),'Dot',fontname='helv')
            page=candidate.new_page();page.insert_text((72,100),'Dot',fontname='tiro')
            result=compare_anchor_geometry(reference,candidate,[{'paragraph':0,'reference_page':1,'candidate_page':1,'anchor':'Dot'}])
            self.assertEqual(result['positive_opacity_font_name_changed_characters'],3)
            self.assertEqual(result['nonpainting_font_name_changed_characters'],0)

    def test_coincident_different_fonts_retain_separate_paint_states(self):
        with fitz.open()as document:
            page=document.new_page();page.insert_text((72,100),'A',fontname='helv')
            page.insert_text((72,100),'A',fontname='tiro',fill_opacity=0)
            text,characters=page_text_geometry(page)
            # MuPDF can deduplicate coincident raw text, but its paint traces
            # still distinguish both fonts at the same coordinates.
            self.assertEqual(text,'A')
            self.assertEqual(characters[0][4],'paint_requested')
            self.assertCountEqual(_text_paint_states(page).values(),['paint_requested','nonpainting'])

    def test_optional_trace_budget_keeps_semantic_measurement_with_unknown_state(self):
        with fitz.open()as reference,fitz.open()as candidate:
            reference.new_page().insert_text((72,100),'Dot',fontname='helv')
            page=candidate.new_page();page.insert_text((72,100),'Dot',fontname='tiro')
            trace={'chars':[(65,0,(72,100),(72,90,80,102))]*200001}
            with patch.object(page,'get_texttrace',return_value=[trace]):
                _,characters=page_text_geometry(page)
                self.assertEqual([c[4]for c in characters],['unknown']*3)

    def test_internal_character_drift_and_encoded_font_names_are_distinct(self):
        raw_name = '휴먼명조'.encode('cp949').decode('latin-1')
        before = ('AB', [((60, 100), (60, 90, 70, 102), raw_name, 12),
                         ((70, 100), (70, 90, 80, 102), raw_name, 12)])
        after = ('AB', [((72, 112), (72, 102, 82, 114), '휴먼명조', 12),
                        ((87, 115), (87, 105, 97, 117), '휴먼명조', 12)])
        with patch('magic_converter.core.diagnostics.text_geometry.page_text_geometry', side_effect=[before, after]):
            result = compare_anchor_geometry([None], [None],
                [{'paragraph': 0, 'reference_page': 1, 'candidate_page': 1, 'anchor': 'AB'}])
        self.assertEqual(result['median_signed_origin_delta_pt'], [12, 12])
        self.assertEqual(result['max_absolute_character_origin_residual_pt'], [5, 3])
        self.assertEqual(result['font_name_changed_characters'], 2)
        self.assertEqual(result['encoding_normalized_font_name_changed_characters'], 0)
        self.assertEqual(result['font_name_normalizations'][raw_name], '휴먼명조')

    def test_real_face_change_is_not_hidden_by_encoding_normalization(self):
        with fitz.open() as reference, fitz.open() as candidate:
            reference.new_page().insert_text((72, 100), 'Different face', fontname='helv')
            candidate.new_page().insert_text((72, 100), 'Different face', fontname='tiro')
            result = compare_anchor_geometry(reference, candidate,
                [{'paragraph':0,'reference_page':1,'candidate_page':1,'anchor':'Differentface'}])
            self.assertEqual(result['encoding_normalized_font_name_changed_characters'], len('Differentface'))

    def test_matching_pages_reveal_exact_position_and_font_differences(self):
        with fitz.open() as reference, fitz.open() as candidate:
            reference.new_page().insert_text((72, 100), 'Unique anchor text', fontsize=10)
            candidate.new_page().insert_text((80, 118), 'Unique anchor text', fontsize=12)
            matched = [{'paragraph':0,'reference_page':1,'candidate_page':1,'anchor':'Uniqueanchortext'}]
            result = compare_anchor_geometry(reference, candidate, matched)
            self.assertEqual(result['missing_geometry'], 0)
            self.assertEqual(result['median_absolute_origin_delta_pt'], [8, 18])
            self.assertEqual(result['different_font_declarations'], 1)
            self.assertEqual(result['anchors'][0]['reference']['fonts'][0]['size_pt'], 10)

    def test_normal_multiline_anchor_and_ambiguous_or_absent_text_controls(self):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 100), 'Unique anchor\nsecond line')
            page.insert_text((72, 150), 'duplicate duplicate')
            geometry = page_text_geometry(page)
            located = locate_anchor(geometry, 'Uniqueanchorsecondline')
            self.assertIsNotNone(located)
            self.assertGreater(located['bounds_pt'][3] - located['bounds_pt'][1], 20)
            self.assertIsNone(locate_anchor(geometry, 'duplicate'))
            self.assertIsNone(locate_anchor(geometry, 'absent'))
            self.assertIsNone(locate_anchor(geometry, ''))
            result = compare_anchor_geometry(document, document,
                [{'paragraph':0,'reference_page':1,'candidate_page':1,'anchor':'absent'}])
            self.assertEqual(result['missing_geometry'], 1)
            self.assertEqual(result['median_absolute_origin_delta_pt'], [None, None])

    def test_unchanged_text_has_zero_coordinates_and_identical_fonts(self):
        with fitz.open() as document:
            document.new_page().insert_text((72, 100), 'Same document anchor')
            result = compare_anchor_geometry(document, document,
                [{'paragraph':0,'reference_page':1,'candidate_page':1,'anchor':'Samedocumentanchor'}])
            self.assertEqual(result['median_absolute_origin_delta_pt'], [0, 0])
            self.assertEqual(result['different_font_declarations'], 0)

    def test_common_output_translation_is_separate_from_local_drift(self):
        with fitz.open() as reference, fitz.open() as candidate:
            a, b = reference.new_page(), candidate.new_page()
            matched = []
            for index, (token, dx, dy) in enumerate((('First anchor', 12, 12), ('Second anchor', 12, 12), ('Third anchor', 17, 15))):
                y = 100 + index * 40
                a.insert_text((60, y), token, fontsize=12)
                b.insert_text((60 + dx, y + dy), token, fontsize=12)
                matched.append({'paragraph': index, 'reference_page': 1, 'candidate_page': 1, 'anchor': token.replace(' ', '')})
            result = compare_anchor_geometry(reference, candidate, matched)
            self.assertEqual(result['median_signed_origin_delta_pt'], [12, 12])
            self.assertEqual(result['median_absolute_origin_residual_pt'], [0, 0])
            self.assertEqual(result['max_absolute_origin_residual_pt'], [5, 3])
            self.assertEqual(result['font_name_changed_characters'], 0)
            self.assertEqual(result['anchors'][2]['origin_delta_pt'], [17, 15])

    def test_tiny_size_changes_and_different_faces_are_reported_separately(self):
        with fitz.open() as reference, fitz.open() as candidate:
            reference.new_page().insert_text((72, 100), 'Size control anchor', fontsize=12)
            candidate.new_page().insert_text((72, 100), 'Size control anchor', fontname='tiro', fontsize=12.001)
            result = compare_anchor_geometry(reference, candidate,
                [{'paragraph': 0, 'reference_page': 1, 'candidate_page': 1, 'anchor': 'Sizecontrolanchor'}])
            self.assertEqual(result['font_name_changed_characters'], len('Sizecontrolanchor'))
            self.assertAlmostEqual(result['anchors'][0]['max_absolute_font_size_delta_pt'], .001, places=4)
