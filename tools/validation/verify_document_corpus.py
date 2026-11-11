"""Local corpus evidence: sampled PDF→editable PPTX→native Office PDF pixels.

Differences are measurements, never silently promoted to fidelity passes.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from tools.validation.native_office_export import export_pdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    args = parser.parse_args()
    folder = args.output.resolve(); folder.mkdir(parents=True, exist_ok=False)
    corpus = args.corpus.resolve()
    if folder.is_relative_to(corpus): raise ValueError('Output must be outside original corpus')
    report = {'status':'running', 'version':__version__, 'mode':'frozen' if args.executable else 'source',
              'executable_sha256':sha(args.executable) if args.executable else None,
              'sampling':'first, middle, last page (deduplicated)', 'cases':[],
              'text_measurement':'Non-whitespace native PDF character counts; not semantic equivalence or OCR.',
              'limitations':['Sampled PDF visual roundtrip, not full-page or semantic Office corpus certification.']}
    report_path = folder / 'report.json'
    def save(): report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    env = os.environ.copy(); env['PYTHONUTF8'] = '1'
    prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT/'run_magic_converter.py')]
    def convert(source, action, output):
        result_path = output.with_suffix('.json')
        command = prefix + ['convert', str(source), '--action', action, '--output', str(output),
                            '--timeout','180','--data-dir',str(folder/'profile'), '--report',str(result_path),'--json']
        done = subprocess.run(command, env=env, capture_output=True, timeout=240,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        data = json.loads(result_path.read_text(encoding='utf-8-sig'))
        if done.returncode or not data['ok']: raise RuntimeError(str(data))
        return Path(data['jobs'][0]['outputs'][0]), data['jobs'][0].get('warnings', [])
    try:
        paths = sorted(corpus.glob('*.pdf'))
        if not paths: raise ValueError('Corpus contains no PDF files')
        for index, source in enumerate(paths):
            case_folder = folder / f'case-{index:02}'; case_folder.mkdir()
            case = {'source_name':source.name, 'source_sha256':sha(source)}
            report['cases'].append(case); save()
            try:
                sample = case_folder/'reference.pdf'
                with pymupdf.open(source) as pdf, pymupdf.open() as selected:
                    if pdf.needs_pass: raise ValueError('Encrypted source PDF')
                    pages = sorted({0, len(pdf)//2, len(pdf)-1})
                    case.update(source_pages=len(pdf), sampled_pages=[p+1 for p in pages])
                    for page in pages: selected.insert_pdf(pdf, from_page=page, to_page=page)
                    selected.save(sample)
                deck, notices = convert(sample, 'pptx_editable', case_folder/'editable')
                from defusedxml import ElementTree as ET
                with zipfile.ZipFile(deck) as package:
                    counts=[]
                    for name in sorted(package.namelist()):
                        if not name.startswith('ppt/slides/slide') or not name.endswith('.xml'):continue
                        tree=ET.fromstring(package.read(name))
                        ids=[n.get('id') for n in tree.iter() if n.tag=='{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr']
                        if len(ids)!=len(set(ids)):raise ValueError('Duplicate slide/group shape IDs: '+name)
                        counts.append(len(ids))
                    case['unique_shape_ids_per_slide']=counts
                native = case_folder/'powerpoint.pdf'
                case['native_export'] = export_pdf(deck, native, case_folder/'native-export', args.executable)
                with pymupdf.open(native) as rendered:
                    case['native_searchable_characters'] = [len(''.join(page.get_text().split())) for page in rendered]
                case.update(status='measured', conversion_warnings=notices,
                            visual=compare(sample, native, artifacts=case_folder/'visual'))
                # A same-stem HWPX/PDF is a supplied reference, not an assumed
                # native ground truth; record provenance and differences.
                paired = source.with_suffix('.hwpx')
                if paired.is_file():
                    output, warnings = convert(paired, 'document:pdf', case_folder/'hwpx-pdf')
                    case['paired_hwpx'] = {'source_sha256':sha(paired), 'reference_provenance':'user-supplied same-stem PDF',
                                           'warnings':warnings, 'visual':compare(source, output, artifacts=case_folder/'hwpx-visual')}
                if sha(source) != case['source_sha256']: raise ValueError('Corpus source changed')
            except Exception as error: case.update(status='failed', error=str(error))
            save()
            print(json.dumps({'case':source.name,'status':case['status'],
                              'visual':case.get('visual',{}).get('status')},ensure_ascii=False), flush=True)
        report['status'] = 'passed' if all(c['status']=='measured' for c in report['cases']) else 'failed'
        report['fidelity'] = 'differences' if any(c.get('visual',{}).get('status')!='matched'
            or c.get('paired_hwpx',{}).get('visual',{}).get('status','matched')!='matched' for c in report['cases']) else 'matched_samples'
    except Exception as error: report.update(status='failed', error=str(error))
    save(); return 0 if report['status']=='passed' else 1


if __name__ == '__main__': raise SystemExit(main())
