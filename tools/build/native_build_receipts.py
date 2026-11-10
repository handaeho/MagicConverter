"""Verify bounded local build/functional receipts against actual archive bytes."""
import hashlib,json,math,zipfile
from pathlib import Path


def review_build(receipt,archive,commit,shipped_sha256,toolchain,license_sha256):
    receipt,archive=Path(receipt),Path(archive)
    if receipt.stat().st_size>16*1024**2:raise ValueError('Native receipt size budget')
    data=json.loads(receipt.read_text(encoding='utf-8'))
    if not(data.get('build_executed')is True and data.get('build_succeeded')is True
           and data.get('source_commit')==commit and data.get('rust_version')==toolchain['rust_version']
           and data.get('toolchain_manifest_sha256')==toolchain['manifest_sha256']):raise ValueError('Native build identity/outcome mismatch')
    if data.get('legal_approval')is not False or data.get('complete_corresponding_source')is not False:raise ValueError('Build evidence cannot approve rights/completeness')
    commands=data.get('commands',[])
    for phase in('native-cargo-metadata','native-cargo-build'):
        rows=[r for r in commands if r.get('phase')==phase]
        if len(rows)!=1 or rows[0].get('status')!='passed'or type(rows[0].get('exit'))is not int or rows[0]['exit']!=0:raise ValueError('Missing successful native phase')
        args=rows[0].get('command',[])
        if not all(isinstance(v,str)for v in args)or not {'--offline','--locked'}<=set(args):raise ValueError('Unlocked native command')
        if phase=='native-cargo-build':
            if not {'build','--release','--bin','rhwp','--target','x86_64-pc-windows-msvc'}<=set(args):raise ValueError('Native recipe differs')
            if (args[1]!='build' or args.count('--bin')!=1 or args[args.index('--bin')+1]!='rhwp'
                    or args.count('--target')!=1 or args[args.index('--target')+1]!='x86_64-pc-windows-msvc'
                    or not set(args[2:])<={'--offline','--locked','--release','--bin','rhwp','--target','x86_64-pc-windows-msvc','-vv','--verbose'}):raise ValueError('Native recipe option binding differs')
    if not data.get('resolved_features')or data.get('source_preservation',{}).get('status')!='passed':raise ValueError('Native feature/source preservation evidence missing')
    preservation=data['source_preservation']
    if preservation.get('git_files')!=1726 or preservation.get('registry_files')!=18234:raise ValueError('Native source preservation counts mismatch')
    features=data['resolved_features']
    if not isinstance(features,dict)or not all(isinstance(k,str)and isinstance(v,list)and all(isinstance(f,str)for f in v)for k,v in features.items()):raise ValueError('Invalid native features')
    experiment=data.get('profile_experiment')
    if experiment:
        overrides=experiment.get('overrides',{});env=data.get('environment',{})
        if (env.get('CARGO_PROFILE_RELEASE_LTO')!=str(overrides.get('lto')).lower()
                or env.get('CARGO_PROFILE_RELEASE_CODEGEN_UNITS')!=str(overrides.get('codegen_units'))):raise ValueError('Native profile declaration/environment mismatch')
    with zipfile.ZipFile(archive)as z:
        members=z.infolist();names=[e.filename for e in members]
        if len(names)!=len(set(names))or set(names)!={'rhwp.exe','review.json','LICENSE'}or sum(e.file_size for e in members)>128*1024**2:raise ValueError('Native archive member/size mismatch')
        if z.getinfo('review.json').file_size>16*1024**2:raise ValueError('Native functional receipt size budget')
        if hashlib.sha256(z.read('LICENSE')).hexdigest()!=license_sha256:raise ValueError('Native license archive mismatch')
        exe=z.read('rhwp.exe');functional=json.loads(z.read('review.json'))
    digest=hashlib.sha256(exe).hexdigest()
    if not exe or digest!=data.get('executable_sha256')or digest!=functional.get('rebuilt_sha256')or functional.get('shipped_sha256')!=shipped_sha256:raise ValueError('Native actual binary hash mismatch')
    identical=digest==shipped_sha256
    if (functional.get('byte_identical_to_shipped')is not identical or functional.get('source_commit')!=commit
            or functional.get('status')!='qualified_functional_correspondence_recorded'):raise ValueError('Native functional identity mismatch')
    cases=functional.get('cases',[])
    if len(cases)<3 or not any(c.get('case')=='actual33'for c in cases):raise ValueError('Native functional controls missing')
    names=[c.get('case')for c in cases]
    if len(names)!=len(set(names))or not {'paragraph','roman','actual33'}<=set(names):raise ValueError('Duplicate/missing native control identities')
    pages=0;strict=True
    for c in cases:
        outputs=c.get('outputs',{});counts=[outputs.get(n,{}).get('pages',0)for n in('shipped','rebuilt')]
        mae=c.get('max_mae');geometry=c.get('geometry_equal')
        if (c.get('status')!='passed_functional'or counts[0]!=counts[1]or any(type(n)is not int for n in counts)or counts[0]<=0
                or type(mae)not in(int,float)or not math.isfinite(mae)or mae<0 or geometry is not True):raise ValueError('Invalid native functional evidence')
        pages+=counts[0];strict &= mae==0 and c.get('strict_visual_status')=='matched'
    if pages<35:raise ValueError('Insufficient native page evidence')
    if functional.get('legal_approval')is not False or functional.get('complete_corresponding_source')is not False:raise ValueError('Functional evidence cannot approve rights/completeness')
    return {'status':'build_receipt_verified','build_executed':True,'review_invocation_compiles':False,
        'source_commit':commit,'rust_version':data['rust_version'],'receipt_sha256':hashlib.sha256(receipt.read_bytes()).hexdigest(),
        'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'rebuilt_sha256':digest,'shipped_sha256':shipped_sha256,
        'byte_identical':identical,'binary_source_correspondence':identical,'functional_controls':len(cases),'functional_pages':pages,
        'strict_functional_pixel_match':strict,'profile_experiment':experiment,'native_pin_replaced':False,
        'complete_corresponding_source':False,'legal_approval':False,
        'scope':'Verified local receipt and actual EXE bytes; functional samples are separate from exact publisher feature/dynamic-input correspondence and rights'}
