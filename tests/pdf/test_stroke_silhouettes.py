"""Stroke geometry, conservative selection and all-or-nothing fallback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.pdf.pdf_pptx_glyphs import _stroke_fill, _UnsupportedOutline


class StrokeSilhouetteTests(unittest.TestCase):
    def test_crossing_stroke_has_no_parity_rule_holes(self):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QPainterPath, QPainterPathStroker
        from magic_converter.core.pdf.pdf_pptx_graphics import path_commands
        points=[fitz.Point(10,10),fitz.Point(30,30),fitz.Point(10,30),fitz.Point(30,10)]
        original=QPainterPath();original.moveTo(*points[0])
        for p in points[1:]:original.lineTo(*p)
        stroker=QPainterPathStroker();stroker.setWidth(4)
        expected=stroker.createStroke(original);expected.setFillRule(Qt.WindingFill)
        items=_stroke_fill([('l',a,b)for a,b in zip(points,points[1:])],4,2,2,10,[30000])
        candidate=QPainterPath();candidate.setFillRule(Qt.OddEvenFill)
        for command,ps in path_commands({'items':items}):
            if command=='M':candidate.moveTo(*ps[0])
            elif command=='L':candidate.lineTo(*ps[0])
            elif command=='C':candidate.cubicTo(*(v for p in ps for v in p))
            else:candidate.closeSubpath()
        for x in range(33,128):
            for y in range(33,128):
                point=QPointF(x/4+.11,y/4+.07)
                self.assertEqual(candidate.contains(point),expected.contains(point),(point.x(),point.y()))

    def test_caps_and_closed_contour_hole(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPainterPath
        from magic_converter.core.pdf.pdf_pptx_graphics import path_commands
        line = [('l', fitz.Point(10, 10), fitz.Point(30, 10))]
        for cap, left in [(0, 10), (1, 8), (2, 8)]:
            items = _stroke_fill(line, 4, cap, 0, 10, [30000])
            xs = [p.x for item in items for p in item[1:]]
            self.assertAlmostEqual(min(xs), left)
            self.assertAlmostEqual(max(xs), 40-left)
        items = _stroke_fill([('re', fitz.Rect(10, 10, 30, 30), 1)], 4, 0, 0, 10, [30000])
        path = QPainterPath(); path.setFillRule(Qt.WindingFill)
        for command, points in path_commands({'items': items}):
            if command == 'M': path.moveTo(points[0].x, points[0].y)
            elif command == 'L': path.lineTo(points[0].x, points[0].y)
            elif command == 'C': path.cubicTo(*(v for p in points for v in p))
            else: path.closeSubpath()
        from PySide6.QtCore import QPointF
        self.assertTrue(path.contains(QPointF(10, 20)))
        self.assertFalse(path.contains(QPointF(20, 20)))

    def test_expansion_budget_and_cancellation_are_not_silent_success(self):
        line = [('l', fitz.Point(10, 10), fitz.Point(30, 10))]
        with self.assertRaises(_UnsupportedOutline):
            _stroke_fill(line, 4, 0, 0, 10, [1])
        from magic_converter.core.jobs.job_control import JobCancelled
        with patch('magic_converter.core.pdf.pdf_pptx_glyphs.check_cancelled', side_effect=JobCancelled('cancel')):
            with self.assertRaises(JobCancelled): _stroke_fill(line, 4, 0, 0, 10, [30000])

    def test_uniform_line_retained_nonuniform_expanded_and_clipped_replayed(self):
        for transformed, clipped, budget in [(False, False, False), (True, False, False),
                                             (True, True, False), (True, False, True)]:
            with self.subTest(transformed=transformed, clipped=clipped, budget=budget), tempfile.TemporaryDirectory() as d:
                source = Path(d)/'stroke.pdf'
                with fitz.open() as doc:
                    p = doc.new_page(width=240, height=160); origin = fitz.Point(40, 90)
                    p.insert_text(origin, 'OO', fontsize=50, render_mode=1)
                    if transformed:
                        stream = doc.get_new_xref(); doc.update_object(stream, '<<>>')
                        doc.update_stream(stream, p.read_contents().replace(b'BT', b'BT 70 Tz')); p.set_contents(stream)
                    if clipped:
                        stream = doc.get_new_xref(); doc.update_object(stream, '<<>>')
                        doc.update_stream(stream, b'q 50 0 190 160 re W n\n'+p.read_contents()+b'\nQ'); p.set_contents(stream)
                    doc.save(source)
                with patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_COMMANDS', 100 if budget else 30000):
                    pdf_to_pptx_editable(source, source.with_suffix('.pptx'))
                shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
                if clipped or budget:
                    self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
                    self.assertFalse(any(s.shape_type == 6 for s in shapes))
                else:
                    self.assertEqual(len(shapes), 1)
                    self.assertEqual(shapes[0].shape_type, 6)
                    for s in shapes[0].shapes:
                        self.assertEqual(bool(s._element.xpath('./p:spPr/a:ln/a:noFill')), transformed)
                        self.assertEqual(bool(s._element.xpath('./p:spPr/a:solidFill')), transformed)


if __name__ == '__main__': unittest.main()
