"""Actual Word/Excel/PowerPoint RSS, separately from converter Job commit."""
import argparse
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from tools.validation.verify_large_input_rss import measure


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--rss-ceiling-mib',type=int,default=1536)
    parser.add_argument('--large',action='store_true',help='Include generated image-heavy DOCX/PPTX inputs')
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    pass
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        import pymupdf as fitz
        inputs=root/'inputs';inputs.mkdir();cases=[]
        for pages in (10,40):
            doc=Document()
            for i in range(pages):
                if i:doc.add_page_break()
                doc.add_paragraph(f'Office RSS word page {i+1}')
                doc.add_paragraph('Normal document content. '*30)
            path=inputs/f'word-{pages}.docx';doc.save(path)
            cases.append((path,f'word-{pages}',pages,f'Office RSS word page {pages}',None,True))
        for rows in (200,2000):
            book=Workbook();sheet=book.active
            for i in range(rows):sheet.append([f'Office RSS row {i+1}',i+1,'Data'])
            sheet.column_dimensions['A'].width=30
            path=inputs/f'excel-{rows}.xlsx';book.save(path);book.close()
            cases.append((path,f'excel-{rows}',None,f'Office RSS row {rows}',None,True))
        for pages in (20,60):
            deck=Presentation()
            for i in range(pages):deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text=f'Office RSS slide {i+1}'
            path=inputs/f'slides-{pages}.pptx';deck.save(path)
            cases.append((path,f'slides-{pages}',pages,f'Office RSS slide {pages}',None,True))
        if args.large:
            import io,random
            from PIL import Image
            from docx.shared import Inches as DocInches
            from pptx.util import Inches as SlideInches
            def image_bytes(seed):
                pixels=random.Random(seed).randbytes(1024*1024*3)
                image=Image.frombytes('RGB',(1024,1024),pixels)
                stream=io.BytesIO();image.save(stream,'PNG',compress_level=0);stream.seek(0)
                return stream
            document=Document()
            for i in range(12):
                if i:document.add_page_break()
                document.add_paragraph(f'Large Office page {i+1}')
                document.add_picture(image_bytes(i),width=DocInches(5))
            path=inputs/'word-image-heavy.docx';document.save(path)
            cases.append((path,'word-image-heavy',12,'Large Office page 12',None,True))
            deck=Presentation()
            for i in range(24):
                slide=deck.slides.add_slide(deck.slide_layouts[5]);slide.shapes.title.text=f'Large Office slide {i+1}'
                slide.shapes.add_picture(image_bytes(100+i),SlideInches(1),SlideInches(1.5),width=SlideInches(5))
            path=inputs/'slides-image-heavy.pptx';deck.save(path)
            cases.append((path,'slides-image-heavy',24,'Large Office slide 24',None,True))
        cases.append((cases[1][0],'deadline',None,'',.3,False))
        cases.append((cases[0][0],'recovery',10,'Office RSS word page 10',None,True))
        originals={str(p):sha(p) for p, *_ in cases}
        prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
        old={k:os.environ.get(k) for k in ('TMP','TEMP','PYTHONUTF8')}
        try:
            os.environ['PYTHONUTF8']='1'
            for source,label,pages,text,timeout,ok in cases:
                scratch=root/(label+'-temp');scratch.mkdir()
                os.environ['TMP']=os.environ['TEMP']=str(scratch)
                output=root/(label+'-output')
                command=prefix+['convert',str(source),'--action','document:pdf','--output',str(output),
                    '--data-dir',str(root/'profile'),'--timeout',str(timeout if timeout is not None else 180),'--json']
                case=measure(command,root/label,expected_ok=ok,deadline=240,office_directory=scratch)
                case['case']=label;case['input_bytes']=source.stat().st_size;report['cases'].append(case);save()
                if case['sampled_tree_and_external_office_peak_rss_bytes']>report['benchmark_total_rss_ceiling_bytes']:
                    raise ValueError('Explicit Office benchmark RSS ceiling exceeded')
                if ok:
                    if not case['office_observation']['processes']:raise ValueError('No identity-checked new Office server observed')
                    if any(p.get('native_commit_limit_bytes')!=2*1024**3 for p in case['office_observation']['processes']):
                        raise ValueError('New native server has no enforced commit-budget receipt')
                    if any('대체' in w or '실패' in w for w in case['jobs'][0].get('warnings',[])):raise ValueError('Native route fell back')
                    with fitz.open(case['outputs'][0]['path']) as pdf:
                        case['verified_pages']=len(pdf)
                        if pages is not None and len(pdf)!=pages:raise ValueError('Office page count differs')
                        if text not in '\n'.join(p.get_text() for p in pdf):raise ValueError('Last Office text missing')
                elif not case['jobs'][0].get('timed_out') or (output.exists() and list(output.iterdir())):
                    raise ValueError('Deadline did not reject without publication')
                save();print(json.dumps({'case':label,'rss':case['sampled_tree_and_external_office_peak_rss_bytes'],'status':'passed'}),flush=True)
        finally:
            for key,value in old.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
        if any(sha(Path(p))!=h for p,h in originals.items()):raise ValueError('Input changed')
        report.update(status='passed',original_hashes=originals)
    except Exception as error:report.update(status='failed',error=str(error))
    save();return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
