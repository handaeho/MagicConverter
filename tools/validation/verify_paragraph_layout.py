"""Declared paragraph geometry, real Word origins and explicit prior EXE controls."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import read_docx,read_hwpx
from magic_converter.core.documents.paragraph_layout import FIELDS
from magic_converter.core.diagnostics.visual_comparison import sha


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--executable',type=Path);p.add_argument('--baseline',type=Path)
    p.add_argument('--baseline-mode',choices=('loss','preserved'),default='loss')
    p.add_argument('--office',action='store_true');a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'version':__version__,'status':'running','cases':[],
        'executable_sha256':sha(a.executable)if a.executable else None,
        'native_word':'requested'if a.office else'skipped: not requested',
        'scope':'Authored point-valued paragraph indents/spacing; no device-specific raw origin shift'}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def convert(source,folder,target,exe):
        folder.mkdir();prefix=[str(exe.resolve())]if exe else[sys.executable,str(ROOT/'run_magic_converter.py')]
        result=subprocess.run(prefix+['convert',str(source),'--action','document:'+target,'--output',str(folder/'output'),
            '--data-dir',str(out/'profile'),'--report',str(folder/'job.json'),'--json','--timeout','60'],
            capture_output=True,timeout=120,env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(result.stdout+result.stderr)
        job=json.loads((folder/'job.json').read_text(encoding='utf-8-sig'))
        if result.returncode or not job['ok']:raise ValueError(str(job))
        return Path(job['jobs'][0]['outputs'][0])
    def origins(pdf):
        import pymupdf as fitz
        with fitz.open(pdf)as document:
            if len(document)!=1:raise ValueError('Paragraph controls changed page count')
            spans=[s for b in document[0].get_text('dict')['blocks']for l in b.get('lines',[])for s in l['spans']]
            result=[]
            for text in('First origin control','Second origin control'):
                selected=[s for s in spans if s['text'].strip()==text]
                if len(selected)!=1:raise ValueError('Authored text control missing or split')
                result.append(list(selected[0]['origin']))
            return result
    save()
    try:
        from docx import Document
        from docx.shared import Pt
        for name,values in [('default',(0,0,0,0,0)),('authored',(18,9,-6,12,8)),('positive-first-line',(12,6,9,6,10))]:
            folder=out/name;folder.mkdir();source=folder/'source.docx';doc=Document()
            for text in('First origin control','Second origin control'):
                paragraph=doc.add_paragraph();run=paragraph.add_run(text);run.font.name='Arial';run.font.size=Pt(12)
                for field,value in zip(FIELDS,values):setattr(paragraph.paragraph_format,field,Pt(value))
            doc.save(source);original=sha(source)
            middle=convert(source,folder/'to-hwpx','hwpx',a.executable)
            target=convert(middle,folder/'to-docx','docx',a.executable)
            case={'case':name,'input_sha256':original,'expected_pt':dict(zip(FIELDS,values)),'status':'running'}
            report['cases'].append(case);save()
            for data in(read_hwpx(middle),read_docx(target)):
                if len(data.blocks)!=2:raise ValueError('Paragraph count changed')
                for paragraph in data.blocks:
                    if any(abs(getattr(paragraph,f)-v)>.051 for f,v in zip(FIELDS,values)):
                        raise ValueError('Authored paragraph layout lost')
            if a.baseline:
                previous=convert(source,folder/'baseline','hwpx',a.baseline)
                case['baseline_layout_pt']={f:getattr(read_hwpx(previous).blocks[0],f)for f in FIELDS}
                preserved=all(abs(case['baseline_layout_pt'][f]-case['expected_pt'][f])<=.051 for f in FIELDS)
                case['baseline_mode']=a.baseline_mode
                if a.baseline_mode=='preserved'and not preserved:
                    raise ValueError('Prior preserved paragraph layout regressed')
                if a.baseline_mode=='loss'and name!='default'and preserved:
                    raise ValueError('Prior EXE did not reproduce paragraph loss')
            if a.office:
                from tools.validation.native_office_export import export_pdf
                for label,path in(('source',source),('returned',target)):
                    pdf=folder/(label+'.pdf')
                    case[label+'_export']=export_pdf(path,pdf,folder/(label+'-export'),a.executable)
                    case[label+'_origins_pt']=origins(pdf)
                error=max(abs(x-y)for a0,b0 in zip(case['source_origins_pt'],case['returned_origins_pt'])for x,y in zip(a0,b0))
                case['max_raw_origin_error_pt']=error
                if error>.1:raise ValueError('Real Word paragraph origin/spacing changed')
            if sha(source)!=original:raise ValueError('Original control modified')
            case['status']='passed';save()
        report.update(status='passed',native_word='passed'if a.office else'skipped: not requested')
    except Exception as e:report.update(status='failed',error=type(e).__name__+': '+str(e))
    save();print(json.dumps(report,ensure_ascii=True));return int(report['status']!='passed')


if __name__=='__main__':raise SystemExit(main())
