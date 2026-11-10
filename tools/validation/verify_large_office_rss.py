"""Generated larger DOCX/XLSX controls, explicitly separate from real corpus."""
import argparse,io,json,os,random,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path,required=True)
    a=p.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'input_kind':'generated, not actual user large DOCX/XLSX','cases':[]}
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        from PIL import Image
        from docx import Document
        from docx.shared import Inches
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as SheetImage
        from pptx import Presentation
        inputs=root/'inputs';inputs.mkdir()
        def png(seed):
            image=Image.frombytes('RGB',(1024,1024),random.Random(seed).randbytes(1024*1024*3))
            stream=io.BytesIO();image.save(stream,'PNG',compress_level=0);stream.seek(0);return stream
        document=Document()
        for index in range(24):
            if index:document.add_page_break()
            document.add_paragraph(f'Large DOCX page {index+1}')
            document.add_picture(png(index),width=Inches(5))
        document.save(inputs/'large.docx')
        book=Workbook();book.remove(book.active)
        for index in range(12):
            sheet=book.create_sheet(f'Image{index+1}')
            sheet.append([f'Large XLSX sheet {index+1}'])
            for row in range(2,6):sheet.append([row,'Normal cells',index])
            image=SheetImage(png(100+index));image.width=image.height=500;sheet.add_image(image,'A8')
            sheet.print_area='A1:M46';sheet.sheet_properties.pageSetUpPr.fitToPage=True
            sheet.page_setup.orientation='portrait';sheet.page_setup.paperSize=sheet.PAPERSIZE_A4
            sheet.page_setup.fitToWidth=1;sheet.page_setup.fitToHeight=1
        book.save(inputs/'large.xlsx');book.close()
        deck=Presentation();deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text='Small PPTX normal control'
        deck.save(inputs/'small-control.pptx')
        command=[sys.executable,'-B',str(ROOT/'tools/validation/verify_office_corpus_rss.py'),
            '--corpus',str(inputs),'--executable',str(a.executable.resolve()),'--output',str(root/'measurement')]
        with (root/'measurement.log').open('wb') as log:
            result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'PYTHONUTF8':'1'},timeout=900)
        measured=json.loads((root/'measurement/report.json').read_text(encoding='utf-8'))
        report.update(inputs=measured['inputs'],cases=measured['cases'],original_hashes_unchanged=measured.get('original_hashes_unchanged'))
        if result.returncode or measured['status']!='passed':raise ValueError(str(measured.get('error','Measurement failed')))
        for suffix,pages,last in [('.docx',24,'Large DOCX page 24'),('.xlsx',12,'Large XLSX sheet 12')]:
            case=next(c for c in report['cases'] if c['source'].endswith(suffix) and c['route']=='direct-com')
            import pymupdf as fitz
            with fitz.open(case['outputs'][0]['path']) as pdf:
                if len(pdf)!=pages or last not in ''.join(page.get_text() for page in pdf):raise ValueError('Native large control truncated')
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();print(json.dumps({'status':report['status'],'cases':len(report['cases']),'error':report.get('error')}),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
