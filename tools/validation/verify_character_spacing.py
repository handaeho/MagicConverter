"""Verified uniform-advance tracking through actual model/CLI and Word output."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,write_hwpx,read_hwpx,read_docx,write_docx
from magic_converter.core.diagnostics.visual_comparison import sha


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path);p.add_argument('--baseline',type=Path);p.add_argument('--office',action='store_true')
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    r={'status':'running','version':__version__,'executable_sha256':sha(a.executable)if a.executable else None,'cases':[],
       'native_word':'requested'if a.office else'skipped','scope':'Explicit installed faces, uniform Hangul-syllable advances, width100. Latin controls retain a tracking-loss warning; not arbitrary shaping or device origin equivalence'}
    def save():(out/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
    def convert(source,folder,target,exe=None):
        folder.mkdir();prefix=[str(exe.resolve())]if exe else[sys.executable,'-B',str(ROOT/'run_magic_converter.py')]
        with(folder/'conversion.log').open('wb')as log:
            done=subprocess.run(prefix+['convert',str(source),'--action','document:'+target,'--output',str(folder/'output'),
                '--report',str(folder/'job.json'),'--data-dir',str(out/'profile'),'--timeout','60','--json'],stdout=log,stderr=subprocess.STDOUT,
                timeout=120,env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job=json.loads((folder/'job.json').read_text(encoding='utf-8-sig'));assert done.returncode==0 and job['ok'],job
        return Path(job['jobs'][0]['outputs'][0])
    def origins(pdf,text):
        import pymupdf as fitz
        with fitz.open(pdf)as doc:
            chars=[c for b in doc[0].get_text('rawdict')['blocks']for l in b.get('lines',[])for s in l['spans']for c in s['chars']if not c['c'].isspace()]
            assert len(doc)==1 and ''.join(c['c']for c in chars)==text
            return [c['origin'][0]for c in chars]
    save()
    try:
        for name,text,font,tracking in [('default','가나다라','굴림',0),('negative','가나다라','굴림',-15),('positive','가나다라','굴림',20),
                ('fractional-negative','가나다라'*10,'굴림',-17),('fractional-positive','가나다라'*10,'굴림',13),
                ('narrow','IIII','Arial',20),('wide','WWWW','Arial',20)]:
            f=out/name;f.mkdir();source=f/'source.hwpx';run=TextRun(text,font=font,size=12,spacing_percent=tracking)
            write_hwpx(DocumentData([Paragraph([run])],width=1000),source);before=sha(source)
            result=convert(source,f/'docx','docx',a.executable);data=read_docx(result);actual=data.blocks[0].runs[0]
            from magic_converter.core.documents.character_spacing import docx_spacing
            points=docx_spacing(run);supported=points is not None
            expected=points if supported else 0
            values=[r.spacing_pt for r in data.blocks[0].runs for c in r.text]
            assert ''.join(r.text for r in data.blocks[0].runs)==text
            assert all(abs(value-expected)<=.051 for value in values)
            cumulative=[];balance=0
            for value in values:balance+=value-expected;cumulative.append(abs(balance))
            assert max(cumulative,default=0)<=.025001
            returned=convert(result,f/'hwpx','hwpx',a.executable);back_model=read_hwpx(returned);back=back_model.blocks[0].runs[0]
            if supported:
                assert all(abs(docx_spacing(r)-points)<=.051 for r in back_model.blocks[0].runs)
            else:
                assert back.spacing_percent==0
                job=json.loads((f/'docx/job.json').read_text(encoding='utf-8-sig'))
                assert any('자간'in w for w in job['jobs'][0].get('warnings',[]))
            case={'case':name,'text':text,'font':font,'tracking_percent':tracking,'word_spacing_pt':actual.spacing_pt,
                  'word_spacing_segments_pt':values,'model_max_cumulative_error_pt':max(cumulative,default=0),
                  'roundtrip_percent':back.spacing_percent,'input_sha256':before,'tracking_conversion':'preserved'if supported else'unsupported_with_loss_warning'}
            if a.baseline:
                old=convert(source,f/'baseline','docx',a.baseline)
                from docx import Document
                from docx.oxml.ns import qn
                pr=Document(old).paragraphs[0].runs[0]._r.rPr;node=pr.find(qn('w:spacing'))if pr is not None else None
                case['baseline_word_spacing_pt']=int(node.get(qn('w:val')))/20 if node is not None else 0
                old_data=read_docx(old);total=0;old_errors=[]
                for old_run in old_data.blocks[0].runs:
                    for c in old_run.text:total+=old_run.spacing_pt-expected;old_errors.append(abs(total))
                case['baseline_model_max_cumulative_error_pt']=max(old_errors,default=0)
            if a.office:
                from tools.validation.native_office_export import export_pdf
                base=f/'unspaced.docx';write_docx(DocumentData([Paragraph([TextRun(text,font=font,size=12)])],width=1000),base)
                native=f/'native.pdf';reference=f/'unspaced.pdf'
                case['native_export']=export_pdf(result,native,f/'native-export',a.executable)
                case['baseline_export']=export_pdf(base,reference,f/'unspaced-export',a.executable)
                x,y=origins(native,text),origins(reference,text)
                errors=[abs((b-x[0])-(c-y[0])-n*expected)for n,(b,c)in enumerate(zip(x,y))]
                case['native_increment_errors_pt']=errors
                steps=[abs((x[n]-x[n-1])-(y[n]-y[n-1])-expected)for n in range(1,len(x))]
                case['native_step_errors_pt']=steps
                case['native_quantization_scope']='Per-gap error bounded by0.051pt; cumulative raw origin error is reported separately and is not zeroed/aligned'
                assert max(steps)<=.051,steps
                assert max(errors)<=.1,errors
            assert sha(source)==before
            case['status']='passed';r['cases'].append(case);save()
        r.update(status='passed',native_word='passed'if a.office else'skipped')
    except Exception as e:r.update(status='failed',error=type(e).__name__+': '+str(e))
    save();print(json.dumps(r,ensure_ascii=True));return int(r['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
