"""Actual XLSX full-content PDF audit, with bounded process/memory observations.

Cell addresses and every non-whitespace value character are hashed in order.
This verifies the data fallback, not Excel print-layout fidelity.
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from tools.validation.verify_large_input_rss import measure


def add(digest,sheet,address,value):
    data=json.dumps([sheet,address,''.join(value.split())],ensure_ascii=False,separators=(',',':'))
    digest.update(data.encode('utf-8')+b'\n')


def source_contents(source):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    digest=hashlib.sha256();sheets=rows=cells=formulas=0
    book=load_workbook(source,read_only=True,data_only=False,keep_links=False)
    try:
        for sheet in book:
            sheets+=1;sheet.reset_dimensions()
            for row,values in enumerate(sheet.iter_rows(values_only=True),1):
                if not any(value is not None for value in values):continue
                rows+=1
                for column,value in enumerate(values,1):
                    if value is None:continue
                    cells+=1;formulas+=isinstance(value,str)and value.startswith('=')
                    add(digest,sheet.title,f'{get_column_letter(column)}{row}',str(value))
    finally:book.close()
    return {'sheets':sheets,'rows':rows,'cells':cells,'formulas':formulas,'content_sha256':digest.hexdigest()}


def pdf_contents(path,folder):
    import pymupdf as fitz
    digest=hashlib.sha256();cells=0;sheet=address=None;pieces=[];next_part=0;actualtext_pages=0
    def flush():
        nonlocal cells
        if address is not None:add(digest,sheet,address,''.join(pieces));cells+=1
    with fitz.open(path)as pdf:
        if not len(pdf):raise ValueError('Empty PDF')
        for page in pdf:
            marked=[]
            contents=page.get_contents()
            for xref in contents:
                stream=pdf.xref_stream(xref)
                if b'/MagicWorkbookText'not in stream:continue
                match=re.fullmatch(rb'/Span << /ActualText <([0-9a-f]+)> /MagicWorkbookText true >> BDC\n(.+)\nEMC\n',stream,re.S)
                if len(contents)!=1 or match is None:
                    raise ValueError('Invalid or partially covered workbook ActualText')
                data=bytes.fromhex(match[1].decode('ascii'))
                if not data.startswith(b'\xfe\xff'):raise ValueError('Missing workbook ActualText byte order mark')
                marked.append(data[2:].decode('utf-16-be'))
            if marked:
                if len(marked)!=1:raise ValueError('Duplicate workbook ActualText')
                lines=marked[0].splitlines();actualtext_pages+=1
            else:lines=page.get_text().splitlines()
            if not lines or not (header:=re.fullmatch(r'(.*?) / \d+-\d+ / Cell values and formulas',lines[0])):
                raise ValueError(f'Page {page.number+1}: missing data heading')
            title=header[1]
            for line in lines[1:]:
                if line=='Empty worksheet':continue
                match=re.fullmatch(r'([A-Z]+\d+)(?:\+(\d+))?(?: (.*))?',line)
                if not match:raise ValueError(f'Page {page.number+1}: unexpected cell line')
                part=int(match[2]or 0)
                if part==0:
                    flush();sheet=title;address=match[1];pieces=[];next_part=0
                if sheet!=title or address!=match[1]or part!=next_part:
                    raise ValueError(f'Page {page.number+1}: skipped/reordered continuation')
                pieces.append(match[3]or'');next_part+=1
        flush()
        selected=sorted({0,len(pdf)//2,len(pdf)-1})
        for index in selected:pdf[index].get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(folder/f'page-{index+1:06}.png')
        return {'pages':len(pdf),'cells':cells,'content_sha256':digest.hexdigest(),'rendered_pages':[i+1 for i in selected],
                'actualtext_pages':actualtext_pages,
                'text_scope':'Whole-page standard ActualText for shaped pages; visible extracted text for all other pages. No Unicode normalization or inferred character repair.'}


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path)
    p.add_argument('--timeout',type=int,default=3600,
                   help='Conversion deadline in seconds, matching the application default')
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    source=a.source.resolve();before=sha(source)
    report={'status':'running','version':__version__,'source':str(source),'source_sha256':before,
            'requested_timeout_seconds':a.timeout,
            'executable_sha256':sha(a.executable)if a.executable else None,
            'scope':'All cell addresses and non-whitespace value/formula characters, in original sheet/row/column order. Print layout, styles and drawings are not preserved.',
            'rss_scope':'25ms samples of the owned process tree; may miss brief peaks or count shared pages twice.'}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        report['expected']=source_contents(source);save()
        prefix=[str(a.executable.resolve())]if a.executable else[sys.executable,'-B',str(ROOT/'run_magic_converter.py')]
        command=prefix+['convert',str(source),'--action','document:pdf','--output',str(out/'output'),
                        '--data-dir',str(out/'profile'),'--timeout',str(a.timeout),'--json']
        report['conversion']=measure(command,out/'measurement',expected_ok=True,deadline=a.timeout+60);save()
        paths=report['conversion']['outputs'];assert len(paths)==1
        pdf=Path(paths[0]['path']);assert pdf.stat().st_size<=256*1024**2
        report['actual']=pdf_contents(pdf,out)
        assert report['actual']['cells']==report['expected']['cells'],'Cell count differs'
        assert report['actual']['content_sha256']==report['expected']['content_sha256'],'Full content hash differs'
        assert sha(source)==before,'Original changed'
        report.update(status='passed',original_unchanged=True)
    except Exception as error:report.update(status='failed',error=type(error).__name__+': '+str(error),original_unchanged=sha(source)==before)
    save();print(json.dumps(report,ensure_ascii=True));return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
