"""Report source HWPX/reference provenance and page drift without rescaling."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from defusedxml import ElementTree as ET
import pymupdf as fitz
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from magic_converter.core.diagnostics.text_geometry import compare_anchor_geometry


def normal(text): return re.sub(r'\s+','',text)


def anchors(paragraphs, reference_pages, candidate_pages):
    """Only unique full anchors; ambiguous/missing text is never a page match."""
    reference=[normal(p) for p in reference_pages];candidate=[normal(p) for p in candidate_pages]
    tokens=[normal(p)[:48] for p in paragraphs]
    counts=Counter(tokens);matched=[];unmatched=ambiguous=0
    for index,token in enumerate(tokens):
        if len(token)<16: continue
        before=[i+1 for i,p in enumerate(reference) if p.count(token)==1]
        after=[i+1 for i,p in enumerate(candidate) if p.count(token)==1]
        if counts[token]!=1 or len(before)>1 or len(after)>1:
            ambiguous+=1;continue
        if len(before)!=1 or len(after)!=1:
            unmatched+=1;continue
        matched.append({'paragraph':index,'anchor':token,'reference_page':before[0],
                        'candidate_page':after[0],'page_delta':after[0]-before[0]})
    return {'matched':matched,'ambiguous':ambiguous,'unmatched':unmatched,
            'note':'Unique paragraph-prefix anchors locate drift; not semantic equivalence or proof of identical authoring conditions.'}


def inspect(source, reference, candidate):
    with zipfile.ZipFile(source) as archive:
        version=ET.fromstring(archive.read('version.xml')).attrib
        header=ET.fromstring(archive.read('Contents/header.xml'))
        fonts=sorted({n.get('face') for n in header.iter() if n.tag.endswith('}font')})
        paragraphs=[];sizes=[];margins=[]
        for name in sorted(archive.namelist()):
            if not re.fullmatch(r'Contents/section\d+\.xml',name): continue
            section=ET.fromstring(archive.read(name))
            for node in section.iter():
                if node.tag.endswith('}pagePr'):
                    sizes.append(dict(node.attrib))
                    margins.append(next((dict(child.attrib)for child in node if child.tag.endswith('}margin')),{}))
            for node in section:
                if node.tag.endswith('}p'):
                    paragraphs.append(''.join(n.text or '' for n in node.iter() if n.tag.endswith('}t')))
    with fitz.open(reference) as before,fitz.open(candidate) as after:
        measured=anchors(paragraphs,[p.get_text() for p in before],[p.get_text() for p in after])
        return {'status':'measured','fidelity':'different' if len(before)!=len(after) or any(a['page_delta'] for a in measured['matched']) else 'page_anchors_match',
                'reference_provenance':'Caller-supplied reference PDF; same authoring conditions are not assumed.',
                'reference_pages':len(before),'candidate_pages':len(after),
                'reference_sizes_pt':sorted({tuple(p.rect[2:]) for p in before}),
                'candidate_sizes_pt':sorted({tuple(p.rect[2:]) for p in after}),
                'coordinate_frame':'Raw unrotated PDF page points; report MediaBox/CropBox/rotation separately. No inferred 12pt translation or rescaling.',
                'reference_page_frames':[{'page':p.number+1,'media_box_pt':list(p.mediabox),'crop_box_pt':list(p.cropbox),'rotation':p.rotation}for p in before],
                'candidate_page_frames':[{'page':p.number+1,'media_box_pt':list(p.mediabox),'crop_box_pt':list(p.cropbox),'rotation':p.rotation}for p in after],
                'hwpx_writer':version,'hwpx_page_properties':sizes,'declared_fonts':fonts,
                'hwpx_page_margins_hwp_units':margins,
                'reference_metadata':before.metadata,'candidate_metadata':after.metadata,'anchors':measured,
                'text_geometry':compare_anchor_geometry(before,after,measured['matched'])}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--reference',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path)
    pass
    parser.add_argument('--expect-pages',type=int)
    parser.add_argument('--max-shifted-anchors',type=int)
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__}
    def save(): (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        if args.native:
            from magic_converter.core.settings.preferences import default_preferences
            from magic_converter.core.settings.settings_store import SettingsStore
            profile=root/'profile';profile.mkdir()
            pass
        before={str(p.resolve()):sha(p) for p in (args.source,args.reference)}
        env=os.environ.copy();env['PYTHONUTF8']='1'
        prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
        job_path=root/'conversion.json'
        result=subprocess.run(prefix+['convert',str(args.source.resolve()),'--action','document:pdf',
            '--output',str(root/'pdf'),'--data-dir',str(root/'profile'),'--report',str(job_path),'--timeout','240','--json'],
            env=env,capture_output=True,timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job=json.loads(job_path.read_text(encoding='utf-8-sig'))
        if result.returncode or not job['ok']: raise ValueError(str(job))
        candidate=Path(job['jobs'][0]['outputs'][0])
        report.update(inspect(args.source,args.reference,candidate))
        shifted=sum(a['page_delta']!=0 for a in report['anchors']['matched'])
        report['shifted_anchors']=shifted
        if args.expect_pages is not None and report['candidate_pages']!=args.expect_pages:
            raise ValueError('Expected page count differs')
        if args.max_shifted_anchors is not None and shifted>args.max_shifted_anchors:
            raise ValueError('Page anchor drift exceeds explicit limit')
        report['warnings']=job['jobs'][0].get('warnings',[])
        report['native_requested']=args.native
        if args.native and any('대체' in w or 'rhwp' in w for w in report['warnings']):
            pass
        report['original_hashes']=before
        report['executable_sha256']=sha(args.executable) if args.executable else None
        if any(sha(Path(p))!=h for p,h in before.items()): raise ValueError('Original corpus changed')
    except Exception as error: report.update(status='failed',error=str(error))
    save();print(json.dumps({k:v for k,v in report.items() if k!='anchors'},ensure_ascii=True,indent=2))
    return 0 if report['status']=='measured' else 1


if __name__=='__main__': raise SystemExit(main())
