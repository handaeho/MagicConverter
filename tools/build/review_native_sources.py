"""Inventory shipped native inputs and partial upstream evidence, never approve release."""
import argparse,hashlib,json,re,subprocess,sys,tomllib,zipfile,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from tools.build.build_inventory import checked
from tools.build.prepare_source_review import pinned_vendor_notices

def notice_comparison(source,deployed):
    return {'source_sha256':hashlib.sha256(source).hexdigest(),'deployed_sha256':hashlib.sha256(deployed).hexdigest(),
            'difference':'identical'if source==deployed else'line_endings_only'if source.replace(b'\r\n',b'\n')==deployed.replace(b'\r\n',b'\n')else'content_differs'}

def configuration_flags(text):
    flags=sorted(set(re.findall(r'--[a-z0-9][a-z0-9_-]*(?:=[^\s]+)?',text)))
    if not flags:raise ValueError('Missing FFmpeg configure evidence')
    return {'flags':flags,'gpl_enabled':'--enable-gpl'in flags,'version3_enabled':'--enable-version3'in flags,
            'nonfree_enabled':'--enable-nonfree'in flags,'external_lib_options':[f for f in flags if f.startswith('--enable-lib')]}

def review(release,repo,expected_commit,output,workspace_source=None,crate_cache=None,source_cache=None,native_build_receipt=None,native_build_archive=None):
    from PyInstaller.archive.readers import CArchiveReader
    import win32api
    release=Path(release).resolve();repo=Path(repo).resolve();output=Path(output).resolve()
    if not release.is_relative_to(ROOT/'dist')or not repo.is_relative_to(ROOT/'.build-cache'):
        raise ValueError('Release/source repository must stay inside project artifact roots')
    if not any(output.is_relative_to(ROOT/name)for name in('.test-artifacts','dist')):
        raise ValueError('Review output must stay inside project artifact roots')
    if not re.fullmatch('[0-9a-f]{40}',expected_commit):raise ValueError('Expected immutable source commit required')
    git=lambda *args:subprocess.check_output(['git','--git-dir='+str(repo),*args],cwd=ROOT)
    commit=git('rev-parse','HEAD').decode().strip()
    if commit!=expected_commit or git('rev-parse','refs/tags/v0.8.7^{commit}').decode().strip()!=commit:
        raise ValueError('Source tag/commit differs from expected review input')
    if git('remote','get-url','origin').decode().strip()!='https://github.com/edwardkim/rhwp.git':
        raise ValueError('Unexpected source repository origin')
    policy_path=ROOT/'packaging/release-policy.json';policy_bytes=policy_path.read_bytes();policy=json.loads(policy_bytes)
    output.mkdir(parents=True,exist_ok=False)
    report={'status':'running','analysis_project_version':__version__,'purpose':'Internal technical review; no legal sufficiency or corresponding-source completeness declaration'}
    def save():(output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    save()
    try:
        lock=json.loads((ROOT/'packaging/vendor-lock.json').read_text(encoding='utf-8'))
        native=[entry for artifact in lock['artifacts']for entry in artifact['files']if entry['target'].endswith('/rhwp.exe')or entry['target'].endswith('/ffmpeg.exe')or entry['target'].endswith('/ffprobe.exe')]
        for entry in native:
            path=checked(ROOT,entry['target'],entry['sha256'])
            if path.stat().st_size!=entry['bytes'] or path.is_symlink():raise ValueError('Pinned native size/type differs')
        inventories=[]
        for name in('magic_converter.exe','magic_converter-cli.exe'):
            executable=release/name;archive=CArchiveReader(str(executable));names={n.replace('\\','/'):n for n in archive.toc}
            pinned=[]
            for entry in native:
                digest=hashlib.sha256(archive.extract(names[entry['target']])).hexdigest()
                if digest!=entry['sha256']:raise ValueError('Shipped native differs: '+entry['target'])
                pinned.append({'path':entry['target'],'sha256':digest})
            version=win32api.GetFileVersionInfo(str(executable),'\\')
            with executable.open('rb')as stream:exe_hash=hashlib.file_digest(stream,'sha256').hexdigest()
            inventories.append({'file':name,'sha256':exe_hash,'file_version':[version['FileVersionMS']>>16,version['FileVersionMS']&65535,version['FileVersionLS']>>16,version['FileVersionLS']&65535],
                                'native_pins':pinned,'qt_modules':sorted(n for n in names if re.search(r'(^|/)Qt6[^/]*\.dll$',n,re.I))})
        report['actual_executables']=inventories
        # Inspect shipped PE resources, not an inferred aggregate wheel version.
        import pefile
        qt_versions=[];qt_archive=CArchiveReader(str(release/'magic_converter.exe'))
        cli_archive=CArchiveReader(str(release/'magic_converter-cli.exe'))
        cli_names={n.replace('\\','/'):n for n in cli_archive.toc}
        qt_names={n.replace('\\','/'):n for n in qt_archive.toc}
        for path in inventories[0]['qt_modules']:
            data=qt_archive.extract(qt_names[path])
            if path not in cli_names or hashlib.sha256(cli_archive.extract(cli_names[path])).digest()!=hashlib.sha256(data).digest():
                raise ValueError('GUI/CLI Qt native bytes differ: '+path)
            with pefile.PE(data=data,fast_load=True)as pe:
                pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
                fixed=pe.VS_FIXEDFILEINFO[0]
                qt_versions.append({'path':path,'sha256':hashlib.sha256(data).hexdigest(),
                                    'file_version':[fixed.FileVersionMS>>16,fixed.FileVersionMS&65535,fixed.FileVersionLS>>16,fixed.FileVersionLS&65535]})
        report['actual_qt_pe_versions']=qt_versions
        report['module_term_findings']=[{'module':'Qt6VirtualKeyboard.dll','present':any(any(n.endswith('/Qt6VirtualKeyboard.dll')for n in item['qt_modules'])for item in inventories),
            'published_options':'Commercial or GPLv3; do not infer LGPL from the aggregate PySide6 package declaration',
            'reference':'https://doc.qt.io/qt-6/qtvirtualkeyboard-index.html','commercial_entitlement_evidence':'not supplied'}]
        report['pinned_vendor_notices']=pinned_vendor_notices(ROOT)
        ffmpeg=ROOT/'vendor/ffmpeg/ffmpeg.exe'
        for label,args in(('ffmpeg-buildconf',['-hide_banner','-buildconf']),('ffmpeg-license',['-L'])):
            done=subprocess.run([str(ffmpeg),*args],capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if done.returncode:raise ValueError('Pinned FFmpeg diagnostic failed')
            data=done.stdout+done.stderr;(output/(label+'.txt')).write_bytes(data)
            if label=='ffmpeg-buildconf':report['ffmpeg_configuration']=configuration_flags(data.decode('utf-8',errors='replace'))
        options=report['ffmpeg_configuration']
        if not options['gpl_enabled']or not options['version3_enabled']or options['nonfree_enabled']:
            raise ValueError('Actual FFmpeg options do not support the locked GPL-3.0-or-later review declaration')
        source_names=('Cargo.toml','Cargo.lock','LICENSE','THIRD_PARTY_LICENSES.md','src/renderer/pdf.rs','src/renderer/font_decision.rs',
                      'src/renderer/font_environment.rs','src/renderer/page_layout.rs','src/cli/outputs/pdf.rs')
        files=[];contents={};archive_path=output/'rhwp-partial-source-review.zip'
        with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED)as bundle:
            for name in source_names:
                data=git('show',commit+':'+name);contents[name]=data
                info=zipfile.ZipInfo(name,(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;bundle.writestr(info,data)
                files.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        cargo=tomllib.loads(contents['Cargo.toml'].decode('utf-8'));cargo_lock=tomllib.loads(contents['Cargo.lock'].decode('utf-8'))
        if cargo['package']['version']!='0.8.7'or cargo['package']['license']!='MIT':raise ValueError('Upstream project declaration differs')
        source_report={'url':'https://github.com/edwardkim/rhwp/tree/v0.8.7','commit':commit,'partial_snapshot':True,'files':files,
                       'archive_sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest(),'cargo_packages':len(cargo_lock['package']),
                       'path_patches':cargo.get('patch',{}),'workspace_members':cargo.get('workspace',{}).get('members',[]),
                       'binary_source_correspondence':'not established: no release build provenance/rebuild comparison',
                       'root_notice':notice_comparison(contents['LICENSE'],(ROOT/'vendor/rhwp/LICENSE').read_bytes()),
                       'third_party_notice':notice_comparison(contents['THIRD_PARTY_LICENSES.md'],(ROOT/'vendor/rhwp/THIRD_PARTY_LICENSES.md').read_bytes())}
        report['rhwp_source_review']=source_report
        if (workspace_source is None)!=(crate_cache is None):
            raise ValueError('Workspace source and crate cache evidence must be supplied together')
        if workspace_source is not None:
            from tools.build.source_materials import review_materials
            materials=review_materials(repo,commit,workspace_source,crate_cache)
            (output/'source-materials.json').write_text(json.dumps(materials,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            report['rhwp_build_materials']={key:materials[key]for key in('status','commit','archive_sha256','complete_corresponding_source','remaining')}
            report['rhwp_build_materials'].update(files=len(materials['files']),registry_sources=len(materials['registry_sources']))
            report['rhwp_declared_recipe']=materials['declared_release_recipe']
            tools={name:shutil.which(name)for name in('cargo','rustc','cl','cmake','msbuild')}
            report['native_rebuild']={'status':'not_executed_missing_toolchain'if any(tools[n]is None for n in('cargo','rustc','cl'))else'not_executed',
                                      'tools_available':tools,'build_executed':False,'binary_source_correspondence':False}
        if source_cache is not None:
            config=json.loads((ROOT/'packaging/source-review-inputs.json').read_text(encoding='utf-8'))
            if config['schema_version']!=1 or config['complete_corresponding_source']is not False:
                raise ValueError('Source identity inventory cannot certify completeness')
            report['additional_source_archives']=[]
            for entry in config['sources']:
                path=checked(Path(source_cache).resolve(),entry['filename'],entry['sha256'])
                report['additional_source_archives'].append({**entry,'bytes':path.stat().st_size,'status':'verified_review_input'})
            mapping=config.get('qt_module_sources',{})
            known={name:(component,value['version'])for component,value in mapping.items()for name in value['modules']}
            archives={entry['component']:entry for entry in config['sources']}
            report['qt_source_mapping']=[]
            for entry in qt_versions:
                module=entry['path'].rsplit('/',1)[-1]
                if module not in known:raise ValueError('Unreviewed shipped Qt module: '+module)
                component,version=known[module]
                if entry['file_version'][:3]!=list(map(int,version.split('.'))):raise ValueError('Qt source/PE version mismatch: '+module)
                source=archives.get(component)
                if source is not None and source['version']!=version:raise ValueError('Qt archive/module version mismatch')
                report['qt_source_mapping'].append({'module':module,'native_version':version,'component':component,
                    'source_archive':source['filename']if source else None,'source_status':'verified_version_archive; build correspondence unproven'if source else'missing matching source'})
        report['runtime_packages']=[{'name':p['name'],'version':p.get('versionInfo'),'license_declared':p.get('licenseDeclared')}
                                   for p in json.loads((release/'sbom.spdx.json').read_text(encoding='utf-8'))['packages']
                                   if any(token in p['name'].lower()for token in('pymupdf','pyside','shiboken','ffmpeg','rhwp'))]
        pass
        if workspace_source is not None:
            report['blockers']['rhwp']='Selected workspace/vendor/assets Git blobs and locked registry archives plus declared recipe/Rust version/defaults verified; actual compiler/SDK/features, dynamic inputs, native rebuild and shipped-binary correspondence still unestablished'
        if (native_build_receipt is None)!=(native_build_archive is None):raise ValueError('Native receipt/archive must be supplied together')
        if native_build_receipt is not None:
            from tools.build.native_build_receipts import review_build
            toolchain=json.loads((ROOT/'packaging/native-toolchain-review.json').read_text(encoding='utf-8'))
            report['visible_tool_inventory']=report.get('native_rebuild')
            report['native_rebuild']=review_build(native_build_receipt,native_build_archive,commit,
                hashlib.sha256((ROOT/'vendor/rhwp/rhwp.exe').read_bytes()).hexdigest(),toolchain,
                hashlib.sha256((ROOT/'vendor/rhwp/LICENSE').read_bytes()).hexdigest())
            report['blockers']['rhwp']='Actual offline/locked native build receipt and EXE archive verified. Exact publisher profile/features/dynamic inputs and shipped binary correspondence remain separate from functional sample matches and rights review.'
            source_report['binary_source_correspondence']='See native_rebuild actual byte comparison; complete source/rights remain unapproved'
        report['official_references']=['https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright',
            'https://www.qt.io/development/open-source-lgpl-obligations','https://doc.qt.io/qt-6/qtvirtualkeyboard-index.html','https://ffmpeg.org/legal.html',
            'https://github.com/FFmpeg/web/blob/master/src/legal']
        report['policy']={key:policy[key]for key in('public_release_approved','obligations_reviewed','project_license_file')}
        report['policy_unchanged_sha256']=hashlib.sha256(policy_bytes).hexdigest()
        if policy_path.read_bytes()!=policy_bytes:raise ValueError('Release policy changed during review')
        report['status']='reviewed_with_blockers';report['legal_approval']=False;report['complete_corresponding_source']=False
    except Exception as exc:report.update(status='failed',error=f'{type(exc).__name__}: {exc}')
    save();return report

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--release',type=Path,default=ROOT/'dist'/('v'+__version__))
    parser.add_argument('--source-repo',type=Path,required=True);parser.add_argument('--expected-commit',required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workspace-source',type=Path);parser.add_argument('--crate-cache',type=Path)
    parser.add_argument('--source-cache',type=Path)
    parser.add_argument('--native-build-receipt',type=Path);parser.add_argument('--native-build-archive',type=Path)
    args=parser.parse_args();result=review(args.release,args.source_repo,args.expected_commit,args.output,args.workspace_source,args.crate_cache,args.source_cache,args.native_build_receipt,args.native_build_archive)
    print(json.dumps({'status':result['status'],'output':str(args.output),'error':result.get('error')}));return int(result['status']=='failed')
if __name__=='__main__':raise SystemExit(main())
