"""Prepare a local source-review bundle, never an approved public release."""
import argparse,hashlib,json,subprocess,zipfile
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from tools.build.build_inventory import checked,package_metadata,public_gate

SUFFIXES={'.py','.ps1','.spec','.md','.json','.txt','.toml','.xml','.manifest','.csv','.lock','.yml','.yaml','.hwp','.hwpx',
          '.qss','.ui','.svg','.ico','.png','.jpg','.bmp','.rtf','.rst','.html','.css'}
PREFIXES=('magic_converter/','tests/','tools/','packaging/','requirements/','.github/','docs/','licenses/','assets/')
TOP={'LICENSE','NOTICE','README.md','build.ps1','run_magic_converter.py','pyproject.toml','.gitattributes','.gitignore'}
TEMPLATE_NOTICES={'magic_converter/data/hwpx/LICENSE','magic_converter/data/hwpx/NOTICE'}
NOTICE_NAMES={'LICENSE','NOTICE','COPYING','AUTHORS'}
NOTICE_SUFFIXES={'.apache','.bsd','.external','.ijg','.python'}


def selected(name):
    path = Path(name)
    if name in TOP or name in TEMPLATE_NOTICES:
        return True
    if name.startswith('licenses/') and (path.name in NOTICE_NAMES or path.suffix.lower() in NOTICE_SUFFIXES):
        return True
    return name.startswith(PREFIXES) and path.suffix.lower() in SUFFIXES and not name.startswith('docs/reviews/history/')


def pinned_vendor_notices(root=ROOT):
    notices=[]
    lock=json.loads((root/'packaging/vendor-lock.json').read_text(encoding='utf-8'))
    for artifact in lock['artifacts']:
        for entry in artifact['files']:
            name=entry['target'];path=Path(name)
            if (path.suffix.lower()in{'','.txt','.md','.rtf','.docx'}
                    and any(token in path.name.lower()for token in ('license','readme','notice'))):
                if not name.startswith('vendor/')or not(root/name).resolve().is_relative_to(root.resolve()):
                    raise ValueError('Unsafe pinned notice path')
                original=checked(root,name,entry['sha256'])
                if original.is_symlink()or original.stat().st_size!=entry['bytes']:raise ValueError('Pinned notice size/type differs')
                notices.append({'path':name,'sha256':entry['sha256'],'bytes':entry['bytes'],'artifact':artifact['id']})
    return notices


def prepare(output):
    output=Path(output).resolve()
    if not output.is_relative_to((ROOT/'.test-artifacts').resolve())and not output.is_relative_to((ROOT/'dist').resolve()):
        raise ValueError('Source review output must stay in project artifacts')
    output.mkdir(parents=True,exist_ok=False)
    policy_path=ROOT/'packaging/release-policy.json';before=policy_path.read_bytes()
    policy=json.loads(before);files=[]
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode('utf-8').split('\0')
    vendor_notices=pinned_vendor_notices(ROOT)
    pinned={entry['path']:entry for entry in vendor_notices}
    source_names={n for n in names if n and selected(n)}|{entry['path']for entry in vendor_notices}
    archive=output/'source-review.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED)as bundle:
        for name in sorted(source_names):
            path=ROOT/name
            if path.is_symlink()or not path.resolve().is_relative_to(ROOT):raise ValueError('Unsafe source input: '+name)
            if not path.is_file():continue
            data=path.read_bytes()
            if name in pinned and (len(data)!=pinned[name]['bytes']or hashlib.sha256(data).hexdigest()!=pinned[name]['sha256']):
                raise ValueError('Pinned notice changed while preparing source review: '+name)
            info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            bundle.writestr(info,data)
            files.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    packages,_,_=package_metadata(ROOT)
    components=[{'id':p['SPDXID'],'version':p['versionInfo'],'license_declared':p.get('licenseDeclared'),
                 'source_state':'not established by this project-source review bundle'}for p in packages if p['SPDXID']!='SPDXRef-windows']
    gate={'status':'not_run'}
    try:public_gate(ROOT);gate={'status':'passed'}
    except ValueError as exc:gate={'status':'blocked','reason':str(exc)}
    manifest={'schema_version':1,'purpose':'internal review only; not a corresponding-source declaration',
              'project_version':__version__,'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'working_tree_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
              'files':files,'components':components,'pinned_vendor_notices':vendor_notices,'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    if policy_path.read_bytes()!=before:raise ValueError('Release policy changed during preparation')
    report={'status':'prepared_with_blockers'if gate['status']=='blocked'else'prepared',
            'project_version':__version__,'public_gate':gate,'public_release_approved':policy['public_release_approved'],
            'public_windows_executables':policy.get('public_windows_executables',False),
            'project_license':policy.get('project_license'),
            'obligations_reviewed':policy['obligations_reviewed'],'project_license_file':policy['project_license_file'],
            'policy_unchanged_sha256':hashlib.sha256(before).hexdigest(),'source_files':len(files),'components':len(components),'pinned_vendor_notices':len(vendor_notices),
            'archive_sha256':manifest['archive_sha256'],'missing_evidence':policy['review_required'],
            'limitations':['Project license is reported from the owner policy','Native/transitive corresponding sources and rebuild/relink evidence require component review',
                           'No upload, publication, approval or legal sufficiency determination']}
    (output/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(prepare(args.output),indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
