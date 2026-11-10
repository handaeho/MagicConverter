"""Prepare verified offline Cargo inputs; never run upstream build hooks."""
import argparse,hashlib,json,shutil,sys,tarfile,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tools.build.source_materials import review_materials,safe_name


def registry_vendor(records,cache,destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    total,files=0,[]
    for record in records:
        archive=Path(cache)/record['archive']
        with archive.open('rb')as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=record['sha256']:raise ValueError('Registry checksum changed')
        prefix=record['name']+'-'+record['version'];folder=destination/prefix;folder.mkdir()
        checksums={};seen={};aliases=[]
        with tarfile.open(archive,'r:gz')as bundle:
            for member in bundle:
                name=safe_name(member.name.rstrip('/'))
                if name==prefix and member.isdir():continue
                if not name.startswith(prefix+'/'):raise ValueError('Registry path outside package')
                relative=name[len(prefix)+1:];key=relative.casefold()
                if member.issym()or member.islnk()or not(member.isfile()or member.isdir()):raise ValueError('Link/special registry input: '+member.name)
                target=folder/relative
                if not target.resolve().is_relative_to(folder.resolve()):raise ValueError('Registry path escape')
                if member.isfile():
                    total+=member.size
                    if total>1024**3 or len(files)>=50000 or relative=='.cargo-checksum.json':raise ValueError('Registry vendor size/type budget')
                if key in seen:
                    previous=seen[key]
                    if not member.isfile()or previous not in checksums or relative==previous:raise ValueError('Duplicate registry input: '+member.name)
                    if member.size>16*1024**2:raise ValueError('Registry alias comparison budget')
                    data=bundle.extractfile(member).read()
                    if len(data)!=member.size or hashlib.sha256(data).hexdigest()!=checksums[previous]:raise ValueError('Case-colliding registry bytes differ: '+member.name)
                    if not target.exists():
                        with target.open('xb')as stream:stream.write(data)
                    elif not target.samefile(folder/previous):raise ValueError('Ambiguous registry alias')
                    checksums[relative]=checksums[previous];aliases.append({'path':relative,'same_bytes_as':previous})
                    files.append({'path':prefix+'/'+relative,'bytes':member.size,'sha256':checksums[previous],'case_alias_of':prefix+'/'+previous})
                    continue
                seen[key]=relative
                if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
                target.parent.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256();written=0
                with bundle.extractfile(member)as incoming,target.open('xb')as outgoing:
                    while chunk:=incoming.read(1024*1024):
                        written+=len(chunk)
                        if written>member.size:raise ValueError('Registry member size mismatch')
                        outgoing.write(chunk);digest.update(chunk)
                if written!=member.size:raise ValueError('Registry member truncated')
                checksums[relative]=digest.hexdigest();files.append({'path':prefix+'/'+relative,'bytes':written,'sha256':digest.hexdigest()})
        (folder/'.cargo-checksum.json').write_text(json.dumps({'files':checksums,'package':record['sha256']},sort_keys=True),encoding='utf-8')
    return {'packages':len(records),'files':len(files),'bytes':total,'entries':files}


def prepare(repo,commit,workspace,cache,output):
    output=Path(output).resolve()
    if not output.is_relative_to(ROOT/'.test-artifacts'):raise ValueError('Private rebuild preparation must stay in project artifacts')
    materials=review_materials(repo,commit,workspace,cache)
    output.mkdir(parents=True,exist_ok=False);report={'status':'running','source_commit':commit,'build_executed':False,
        'binary_source_correspondence':False,'complete_corresponding_source':False,'legal_approval':False}
    def save():(output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        source=output/'workspace';source.mkdir()
        with zipfile.ZipFile(workspace)as bundle:
            for entry in materials['files']:
                if entry['mode']not in('100644','100755'):raise ValueError('Unsupported nonregular workspace input')
                target=source/safe_name(entry['path']);target.parent.mkdir(parents=True,exist_ok=True)
                data=bundle.read(entry['path'])
                if hashlib.sha256(data).hexdigest()!=entry['sha256']:raise ValueError('Workspace changed during preparation')
                with target.open('xb')as stream:stream.write(data)
        vendor=registry_vendor(materials['registry_sources'],cache,source/'registry-vendor')
        (output/'registry-vendor-manifest.json').write_text(json.dumps(vendor,indent=2),encoding='utf-8')
        config=source/'.cargo/config.toml';config.parent.mkdir(exist_ok=True)
        config.write_text('[source.crates-io]\nreplace-with = "review-vendor"\n[source.review-vendor]\ndirectory = "registry-vendor"\n[net]\noffline = true\n',encoding='utf-8')
        tools={name:shutil.which(name)for name in('cargo','rustc','cl','cmake','msbuild')}
        report.update(status='prepared_offline_inputs_build_not_executed',workspace_files=len(materials['files']),registry_packages=vendor['packages'],
            registry_files=vendor['files'],registry_bytes=vendor['bytes'],recipe=materials['declared_release_recipe'],tools_available=tools,
            limitation='Prepared verified selected sources and Cargo directory source. No compiler invocation, target resolution, SDK/runner match, binary comparison or legal completion. No upstream script executed.')
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-repo',type=Path,required=True);parser.add_argument('--expected-commit',required=True)
    parser.add_argument('--workspace-source',type=Path,required=True);parser.add_argument('--crate-cache',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();report=prepare(args.source_repo,args.expected_commit,args.workspace_source,args.crate_cache,args.output)
    print(json.dumps({k:v for k,v in report.items()if k!='recipe'}));return int(report['status']=='failed')
if __name__=='__main__':raise SystemExit(main())
