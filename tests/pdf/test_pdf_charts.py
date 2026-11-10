"""Generic chart recognition, editable workbooks and conservative fallback.

All fixtures are generated independently of customer files. Arbitrary names,
coordinates, colours, series sizes and numeric scales exercise the same public
conversion entry point used by the wheel and the standalone executable.
"""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from openpyxl import load_workbook
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.oxml.ns import qn

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.pdf.pdf_charts import extract_charts
from magic_converter.core.pdf.pdf_layout import text_spans, extract_tables
from magic_converter.core.engines.office_engine import find_office


FONT=fitz.Font('helv')
BLUE=(.15,.36,.6)
ORANGE=(.85,.4,.16)


def label(page,x,y,text,size=9,color=(.3,.3,.3)):
    page.insert_text((x-FONT.text_length(text,fontsize=size)/2,
                      y+(FONT.ascender+FONT.descender)*size/2),text,
                     fontsize=size,color=color)


def column_fixture(page, values=(4,9,7,16), *, grouped=None, missing=(), low=0, high=20,
                   percentage=False, offset=0, categories=None, combo=False, stacked=False,
                   outside_shape=False, quadrilaterals=False, batched_markers=False):
    names=categories or ['Birch','Cedar','Elm','Fir']
    left,top,pitch,bottom=80+offset,80,55,280
    pos=lambda v:bottom-(v-low)/(high-low)*(bottom-top)
    for v in (low,low+(high-low)/4,low+(high-low)/2,low+3*(high-low)/4,high):
        text=f'{v*100:g}%' if percentage else f'{v:g}'
        label(page,left-18,pos(v),text)
        page.draw_line((left,pos(v)),(left+pitch*len(values),pos(v)),color=(.8,.8,.8),width=.4)
    series=[values] if grouped is None else [values,grouped]
    for slot,series_values in enumerate(series):
        shape=page.new_shape()
        for i,v in enumerate(series_values):
            x=left+pitch*(i+.5)+(slot-.5)*17 if grouped is not None else left+pitch*(i+.5)
            width=14 if grouped is not None else 24
            rect=fitz.Rect(x-width/2,min(pos(0),pos(v)),x+width/2,max(pos(0),pos(v)))
            if quadrilaterals:shape.draw_quad(fitz.Quad(rect.tl,rect.tr,rect.bl,rect.br))
            else:shape.draw_rect(rect)
            if i not in missing:
                label(page,x,pos(v)-10 if v>=0 else pos(v)+10,f'{v*100:g}%' if percentage else f'{v:g}')
            if stacked:
                page.draw_rect((x-width/2,pos(v+2),x+width/2,pos(v)),color=None,fill=ORANGE)
        if outside_shape:shape.draw_rect((430,330,470,360))
        shape.finish(color=None,fill=BLUE if slot==0 else ORANGE);shape.commit()
    for i,name in enumerate(names):label(page,left+pitch*(i+.5),bottom+13,name)
    if grouped is not None:
        for slot,color in enumerate((BLUE,ORANGE)):
            x=left+40+slot*100
            page.draw_rect((x,top+8,x+15,top+13),color=None,fill=color)
            label(page,x+40,top+10.5,'North' if slot==0 else 'South')
    if combo:
        line_values=(16,55,92,134)
        secondary_high=160
        for v in (0,40,80,120,160):label(page,left+pitch*len(values)+18,pos(low+v/secondary_high*(high-low)),str(v))
        points=[fitz.Point(left+pitch*(i+.5),pos(low+v/secondary_high*(high-low))) for i,v in enumerate(line_values)]
        page.draw_polyline(points,color=ORANGE,width=1.6)
        markers=page.new_shape()
        for point,v in zip(points,line_values):
            if batched_markers:markers.draw_circle(point,3)
            else:page.draw_circle(point,3,color=ORANGE,fill=(1,1,1))
            label(page,point.x,point.y-12,str(v))
        if batched_markers:markers.finish(color=ORANGE,fill=(1,1,1));markers.commit()
    return left,top,left+pitch*len(values),bottom


class PdfChartTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.source=self.root/'unrelated market review.pdf'
        self.target=self.root/'restored.pptx'

    def tearDown(self):self.temp.cleanup()

    def document(self):
        doc=fitz.open()
        return doc,doc.new_page(width=700,height=440)

    def convert(self,doc):
        doc.save(self.source);doc.close()
        before=hashlib.sha256(self.source.read_bytes()).digest()
        self.notices=[]
        pdf_to_pptx_editable(self.source,self.target,warning=self.notices.append)
        self.assertEqual(before,hashlib.sha256(self.source.read_bytes()).digest())
        self.deck=Presentation(self.target)
        return [shape.chart for slide in self.deck.slides for shape in slide.shapes if shape.has_chart]

    def test_columns_have_real_data_and_no_duplicate_plot_objects(self):
        doc,page=self.document();plot=column_fixture(page)
        charts=self.convert(doc)
        self.assertEqual(len(charts),1)
        self.assertEqual(list(charts[0].series[0].values),[4,9,7,16])
        self.assertEqual([c.label for c in charts[0].plots[0].categories],['Birch','Cedar','Elm','Fir'])
        self.assertEqual(len(self.deck.slides[0].shapes),1,'Moving/deleting the chart must leave no chart-shaped ghost')
        workbook=load_workbook(io.BytesIO(charts[0].part.chart_workbook.xlsx_part.blob))
        self.assertEqual(workbook.worksheets[0]['B2'].value,4)
        self.assertEqual(workbook['PDF Recovery']['E2'].value,'Read from visible PDF label')
        self.assertFalse(self.notices)

    def test_unlabelled_values_are_marked_estimated_in_workbook_and_warning(self):
        doc,page=self.document();column_fixture(page,missing=(0,2))
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        book=load_workbook(io.BytesIO(charts[0].part.chart_workbook.xlsx_part.blob))
        self.assertIn('Estimated',book['PDF Recovery']['E2'].value)
        self.assertIn('visible',book['PDF Recovery']['E3'].value)
        self.assertTrue(any('2개 값' in notice for notice in self.notices))
        deleted=charts[0]._chartSpace.findall('.//'+qn('c:dLbl')+'/'+qn('c:delete'))
        self.assertEqual(len(deleted),2,'Do not invent visible labels for values absent from the PDF')

    def test_grouped_percentage_columns_preserve_two_series_and_units(self):
        doc,page=self.document()
        column_fixture(page,values=(.2,.4,.3,.7),grouped=(.3,.2,.5,.6),high=1,percentage=True)
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        self.assertEqual(len(charts[0].series),2)
        self.assertEqual(list(charts[0].series[0].values),[.2,.4,.3,.7])
        self.assertEqual(list(charts[0].series[1].values),[.3,.2,.5,.6])
        self.assertEqual(charts[0].value_axis.tick_labels.number_format,'0%')

    def test_negative_columns_use_plot_edge_for_category_labels(self):
        doc,page=self.document();column_fixture(page,values=(-4,8,-7,16),low=-10,high=20)
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        self.assertEqual(list(charts[0].series[0].values),[-4,8,-7,16])
        self.assertEqual(charts[0].value_axis.minimum_scale,-10)

    def test_axis_range_is_not_truncated_to_the_tallest_bar(self):
        doc,page=self.document();column_fixture(page,values=(2,4,7,6),high=100)
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        self.assertEqual(charts[0].value_axis.maximum_scale,100)
        self.assertEqual(list(charts[0].series[0].values),[2,4,7,6])

    def test_dual_axis_combo_contains_bar_line_four_axes_and_editable_values(self):
        doc,page=self.document();column_fixture(page,combo=True)
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        self.assertEqual(len(charts[0].plots),2)
        self.assertEqual(list(charts[0].series[1].values),[16,55,92,134])
        self.assertEqual(len(charts[0]._chartSpace.findall('.//'+qn('c:valAx'))),2)
        replacement=CategoryChartData();replacement.categories=['A','B','C','D']
        replacement.add_series('Edited bars',[5,10,8,17]);replacement.add_series('Edited line',[17,56,93,135])
        charts[0].replace_data(replacement)
        self.deck.save(self.target)
        reopened=next(s.chart for s in Presentation(self.target).slides[0].shapes if s.has_chart)
        self.assertEqual(list(reopened.series[1].values),[17,56,93,135])
        book=load_workbook(io.BytesIO(reopened.part.chart_workbook.xlsx_part.blob))
        self.assertEqual(book.worksheets[0]['C5'].value,135)

    def test_horizontal_bars_restore_categories_and_data(self):
        doc,page=self.document()
        values=[25,60,45,80];names=['West','East','Central','Coast']
        for value in (0,20,40,60,80,100):label(page,130+value*3,275,str(value))
        for i,(value,name) in enumerate(zip(values,names)):
            y=100+i*40
            page.draw_rect((130,y-10,130+value*3,y+10),color=None,fill=BLUE)
            label(page,95,y,name);label(page,130+value*3+12,y,str(value))
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        self.assertEqual(list(charts[0].series[0].values),values)
        self.assertEqual([c.label for c in charts[0].plots[0].categories],names)
        self.assertEqual(charts[0]._chartSpace.find('.//'+qn('c:barDir')).get('val'),'bar')

    def test_two_line_series_and_multiple_charts_per_page(self):
        doc,page=self.document()
        for value in (0,5,10,15,20):label(page,55,280-value*10,str(value))
        for i,name in enumerate(('May','Jun','Jul','Aug')):label(page,80+i*55,293,name)
        for values,color in (((4,9,7,16),BLUE),((2,5,13,11),ORANGE)):
            points=[fitz.Point(80+i*55,280-v*10) for i,v in enumerate(values)]
            page.draw_polyline(points,color=color,width=1.5)
            for point,v in zip(points,values):label(page,point.x,point.y-10,str(v))
        # A second, independently scaled chart has its own marks and categories.
        column_fixture(page,values=(3,8,6,14),offset=330,categories=['P','Q','R','S'])
        charts=self.convert(doc)
        self.assertEqual(len(charts),2)
        self.assertEqual(sorted(len(c.series) for c in charts),[1,2])

    def test_stacks_logarithmic_scales_and_decorative_panels_are_not_guessed(self):
        for kind in ('stacked','log','panels'):
            with self.subTest(kind=kind):
                doc,page=self.document()
                if kind=='stacked':column_fixture(page,stacked=True)
                elif kind=='log':
                    column_fixture(page)
                    # Replace numeric ticks with a visibly nonlinear sequence.
                    for y in (280,230,180,130,80):
                        page.add_redact_annot((40,y-9,72,y+9),fill=(1,1,1))
                    page.apply_redactions()
                    for y,text in zip((280,230,180,130,80),('1','10','100','1000','10000')):label(page,60,y,text)
                else:
                    for i in range(4):
                        page.draw_rect((50+i*90,70,125+i*90,120),color=None,fill=BLUE)
                        label(page,87+i*90,145,'Panel '+str(i))
                charts=self.convert(doc)
                self.assertFalse(charts)
                self.source.unlink();self.target.unlink()

    def test_open_table_recovers_header_first_column_and_missing_borders(self):
        doc,page=self.document();xs=[40,125,240,360];ys=[80,105,140,175,210]
        for x in xs[1:-1]:page.draw_line((x,ys[0]),(x,ys[-1]),color=(.2,.4,.6),width=.4)
        for y in ys[1:]:page.draw_line((xs[0],y),(xs[-1],y),color=(.2,.4,.6),width=.4)
        for col in range(3):page.draw_rect((xs[col],ys[0],xs[col+1],ys[1]),color=None,fill=BLUE)
        for row in range(4):
            for col in range(3):label(page,(xs[col]+xs[col+1])/2,(ys[row]+ys[row+1])/2,f'R{row} C{col}',color=(1,1,1) if row==0 else (0,0,0))
        charts=self.convert(doc);self.assertFalse(charts)
        tables=[s.table for s in self.deck.slides[0].shapes if s.has_table]
        self.assertEqual(len(tables),1)
        self.assertEqual((len(tables[0].rows),len(tables[0].columns)),(4,3))
        self.assertEqual(tables[0].cell(0,0).text,'R0 C0')
        self.assertIsNotNone(tables[0].cell(0,0)._tc.find('.//'+qn('a:lnL')+'/'+qn('a:noFill')))
        self.assertEqual(sum(s.has_text_frame for s in self.deck.slides[0].shapes),0)

    def test_shared_text_operator_keeps_outside_caption_and_declines_chart(self):
        doc,page=self.document();column_fixture(page,missing=(0,))
        stream=doc.get_new_xref();doc.update_object(stream,'<<>>')
        # One text paint contains both a chart label and a distant caption.
        shared=b'\nq BT /helv 9 Tf 1 0 0 1 105 206.5 Tm (4) Tj 1 0 0 1 400 50 Tm (Outside caption) Tj ET Q'
        doc.update_stream(stream,page.read_contents()+shared);page.set_contents(stream)
        charts=self.convert(doc);self.assertFalse(charts)
        self.assertTrue(any(s.has_text_frame and 'Outside caption' in s.text for s in self.deck.slides[0].shapes))

    def test_quadrilateral_bars_are_recognised_without_rectangle_pdf_operators(self):
        doc,page=self.document();column_fixture(page,quadrilaterals=True)
        charts=self.convert(doc);self.assertEqual(len(charts),1)
        self.assertEqual(list(charts[0].series[0].values),[4,9,7,16])

    def test_shared_graphic_operators_and_batched_unsupported_markers_are_preserved(self):
        for kwargs in ({'outside_shape':True},{'combo':True,'batched_markers':True}):
            with self.subTest(kwargs=kwargs):
                doc,page=self.document();column_fixture(page,**kwargs)
                self.assertFalse(self.convert(doc))
                self.assertGreater(len(self.deck.slides[0].shapes),4)
                self.source.unlink();self.target.unlink()

    @unittest.skipUnless(find_office('PowerPoint'),'Desktop PowerPoint is not installed')
    def test_powerpoint_keeps_negative_bar_fill_and_labels_below_plot(self):
        from magic_converter.core.engines.hwp_converter import convert_office_pdf
        doc,page=self.document();column_fixture(page,values=(-4,8,-7,16),low=-10,high=20)
        self.assertEqual(len(self.convert(doc)),1)
        rendered=self.root/'render.pdf'
        convert_office_pdf(self.target,rendered,lambda _:None)
        with fitz.open(self.source) as original,fitz.open(rendered) as result:
            a=original[0].get_pixmap();b=result[0].get_pixmap()
            for x,y in ((107,226),(217,236)):
                self.assertLess(max(abs(c-d) for c,d in zip(a.pixel(x,y),b.pixel(x,y))),5)
            words=result[0].get_text('words')
            self.assertTrue(all(word[1]>280 for word in words if word[4] in {'Birch','Cedar','Elm','Fir'}))


if __name__=='__main__':unittest.main()
