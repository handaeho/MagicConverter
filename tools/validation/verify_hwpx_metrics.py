"""Actual CLI model width preservation; tracking units remain explicitly distinct."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,write_hwpx,read_hwpx,read_docx
from magic_converter.core.diagnostics.visual_comparison import sha


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True)
    p.add_argument('--executable',type=Path);p.add_argument('--baseline',type=Path)
    p.add_argument('--office',action='store_true',default=os.environ.get('MAGIC_CONVERTER_TEST_OFFICE')=='1')
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(a.executable)if a.executable else None,
            'cases':[],'native_office':'requested'if a.office else'skipped: not requested',
            'scope':'Declared metrics and optional native Word width ratios; not complete glyph shaping/layout/origin equivalence'}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def convert(source,folder,target,exe=None):
        prefix=[str(exe.resolve())]if exe else[sys.executable,str(ROOT/'run_magic_converter.py')]
        result=subprocess.run(prefix+['convert',str(source),'--action','document:'+target,'--output',str(folder/'output'),
            '--data-dir',str(out/'profile'),'--report',str(folder/'job.json'),'--json','--timeout','60'],capture_output=True,timeout=120,
            env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(result.stdout+result.stderr)
        job=json.loads((folder/'job.json').read_text(encoding='utf-8-sig'))
        if result.returncode or not job['ok']:raise ValueError(str(job))
        return Path(job['jobs'][0]['outputs'][0]),job['jobs'][0].get('warnings',[])
    save()
    try:
        for name,width,tracking in [('default',100,0),('narrow',80,-15),('wide',130,20)]:
            folder=out/name;folder.mkdir();source=folder/'source.hwpx'
            write_hwpx(DocumentData([Paragraph([TextRun('가나다 ABC',width_percent=width,spacing_percent=tracking)])],
                margins=(36,72,72,72)),source)
            original=sha(source);input_data=read_hwpx(source)
            if input_data.blocks[0].runs[0].spacing_percent!=tracking:raise ValueError('Input tracking lost')
            target,warnings=convert(source,folder,'docx',a.executable);data=read_docx(target)
            if data.text()!='가나다 ABC'or any(r.width_percent!=width for r in data.blocks[0].runs):raise ValueError('Width/text lost')
            if tracking and not any('자간'in w for w in warnings):raise ValueError('Tracking loss was silent')
            back=folder/'roundtrip';back.mkdir();result,_=convert(target,back,'hwpx',a.executable);returned=read_hwpx(result)
            if returned.margins!=(36,72,72,72)or any(r.width_percent!=width for r in returned.blocks[0].runs):raise ValueError('Roundtrip width or declared origin changed')
            case={'case':name,'status':'passed','width_percent':width,'input_tracking_percent':tracking,
                  'output_tracking_percent':data.blocks[0].runs[0].spacing_percent,'warnings':warnings,'input_sha256':original,
                  'declared_left_margin_pt':returned.margins[0]}
            if a.office:
                from tools.validation.native_office_export import export_pdf
                import pymupdf as fitz
                native=folder/'native-word.pdf'
                case['native_export']=export_pdf(target,native,folder/'native-export',a.executable)
                with fitz.open(native)as pdf:
                    spans=[s for b in pdf[0].get_text('rawdict')['blocks']for line in b.get('lines',[])for s in line['spans']]
                    chars=[c for s in spans for c in s['chars']];text=''.join(c['c']for c in chars)
                    expected='가나다 ABC'
                    if len(pdf)!=1 or not text.startswith(expected)or text[len(expected):].strip():raise ValueError('Native Word lost control text')
                    # Word exports an additional paragraph-mark space at the
                    # default width. It is not part of the authored control;
                    # including it makes a valid130% run appear only127.9%.
                    authored=chars[:len(expected)]
                    case['native_pdf_suffix']=text[len(expected):]
                    case['native_full_extraction_width_pt']=max(c['bbox'][2]for c in chars)-min(c['bbox'][0]for c in chars)
                    case['native_text_width_pt']=max(c['bbox'][2]for c in authored)-min(c['bbox'][0]for c in authored)
                    case['native_origin_advance_pt']=authored[-1]['origin'][0]-authored[0]['origin'][0]
                    if case['native_text_width_pt']<=0:raise ValueError('Invalid native width')
            if a.baseline:
                old=folder/'baseline';old.mkdir();target,_=convert(source,old,'docx',a.baseline)
                case['baseline_widths_percent']=[r.width_percent for r in read_docx(target).blocks[0].runs]
            if sha(source)!=original:raise ValueError('Original changed')
            report['cases'].append(case);save()
        if a.office:
            base=report['cases'][0]['native_text_width_pt']
            for case in report['cases']:
                ratio=case['native_text_width_pt']/base;case['native_width_ratio_to_default']=ratio
                advance=case['native_origin_advance_pt']/report['cases'][0]['native_origin_advance_pt']
                case['native_origin_ratio_to_default']=advance
                if abs(ratio-case['width_percent']/100)>.02:raise ValueError('Declared width not reflected in native Word rendering')
                if abs(advance-case['width_percent']/100)>.02:raise ValueError('Declared width not reflected in native Word origins')
            report['native_office']='passed'
        report['status']='passed'
    except Exception as e:report.update(status='failed',error=type(e).__name__+': '+str(e))
    save();print(json.dumps(report,ensure_ascii=True));return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
