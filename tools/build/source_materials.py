"""Verify review materials against immutable Git blobs and Cargo.lock.

This is evidence collection, never a binary/source correspondence certificate.
Archives are read as data; no upstream scripts, links or build hooks run.
"""
import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

BUILD_ROOTS = ('src/', 'crates/', 'vendor/', 'assets/', 'bindings/Native/',
               'tools/rhwp-subsecond/', 'tools/batch-convert/',
               'tools/llm_verifier/verdict_protocol/', 'tools/llm_verifier/claim_bind/',
               'tools/llm_verifier/criteria_decomp/', 'ttfs/', 'template/')


def safe_name(name):
    parts = PurePosixPath(name).parts
    if not name or '\\' in name or ':' in name or name.startswith('/') or '..' in parts:
        raise ValueError('Unsafe source archive member: ' + name)
    return name


def selected_tree(repo, commit):
    config = json.loads((Path(__file__).resolve().parents[2] / 'packaging/source-review-inputs.json').read_text(encoding='utf-8'))
    additional = {safe_name(n) for n in config.get('rhwp_additional_inputs', [])}
    raw = subprocess.check_output(['git', '--git-dir=' + str(repo), 'ls-tree', '-rz', '--full-tree', commit])
    result = {}
    for item in raw.split(b'\0'):
        if not item:
            continue
        meta, name = item.split(b'\t', 1)
        mode, kind, oid = meta.decode().split()
        name = safe_name(name.decode('utf-8'))
        if kind == 'blob' and ('/' not in name or name.startswith(BUILD_ROOTS) or name in additional):
            result[name] = {'mode': mode, 'object_id': oid}
    if additional - result.keys():
        raise ValueError('Declared additional build inputs absent from immutable tree')
    return result


def verify_workspace(archive, expected):
    if not expected or 'Cargo.lock' not in expected or 'Cargo.toml' not in expected:
        raise ValueError('Missing independent build tree evidence')
    records = []
    contents = {}
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        names = [safe_name(i.filename) for i in infos]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('Build source entries missing, duplicated or unexpected')
        if sum(i.file_size for i in infos) > 512 * 1024 * 1024:
            raise ValueError('Build source review size limit')
        for info in infos:
            data = bundle.read(info)
            oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            if oid != expected[info.filename]['object_id']:
                raise ValueError('Git source blob differs: ' + info.filename)
            records.append({'path': info.filename, **expected[info.filename], 'bytes': len(data),
                            'sha256': hashlib.sha256(data).hexdigest()})
            if info.filename in ('Cargo.lock', 'Cargo.toml', 'rust-toolchain.toml', '.github/workflows/release-binary.yml'):
                contents[info.filename] = data
    return records, contents


def verify_crates(lock, cache):
    records = []
    for package in tomllib.loads(lock.decode('utf-8'))['package']:
        if 'source' not in package:
            continue
        if package['source'] != 'registry+https://github.com/rust-lang/crates.io-index':
            raise ValueError('Unsupported source needs separate evidence: ' + package['source'])
        name, version = package['name'], package['version']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or not re.fullmatch(r'[A-Za-z0-9_.+-]+', version):
            raise ValueError('Unsafe Cargo package identity')
        path = Path(cache) / (name + '-' + version + '.crate')
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing pinned crate: ' + str(path))
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != package['checksum']:
            raise ValueError('Pinned crate checksum differs: ' + name)
        with tarfile.open(path, 'r:gz') as bundle:
            member_name = name + '-' + version + '/Cargo.toml'
            matches = [m for m in bundle.getmembers() if m.name == member_name]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 4 * 1024 * 1024:
                raise ValueError('Missing or invalid crate declaration: ' + name)
            declaration = tomllib.loads(bundle.extractfile(matches[0]).read().decode('utf-8'))['package']
            if (declaration.get('name'), declaration.get('version')) != (name, version):
                raise ValueError('Crate identity differs: ' + name)
        records.append({'name': name, 'version': version, 'sha256': digest, 'bytes': path.stat().st_size,
                        'license_declared': declaration.get('license'), 'license_file': declaration.get('license-file'),
                        'archive': path.name, 'url': f'https://static.crates.io/crates/{name}/{path.name}'})
    if not records:
        raise ValueError('No registry source evidence')
    return records


def review_materials(repo, commit, workspace, cache):
    files, contents = verify_workspace(workspace, selected_tree(repo, commit))
    crates = verify_crates(contents['Cargo.lock'], cache)
    manifest = tomllib.loads(contents['Cargo.toml'].decode('utf-8'))
    from tools.build.native_recipe import rhwp_recipe
    return {'status': 'verified_selected_build_materials', 'commit': commit,
            'archive_sha256': hashlib.sha256(Path(workspace).read_bytes()).hexdigest(),
            'files': files, 'registry_sources': crates,
            'workspace_members': manifest['workspace']['members'], 'path_patches': manifest.get('patch', {}),
            'declared_release_recipe': rhwp_recipe(contents),
            'complete_corresponding_source': False, 'legal_approval': False,
            'remaining': ['Bulk tests/corpora/scripts outside selected roots and dynamic/generated references need closure review',
                          'Declared recipe/toolchain/defaults verified; no native rebuild/provenance or actual SDK/compiler/feature resolution and shipped-binary correspondence established',
                          'Registry archive declarations are recorded, not legal permission or complete native build dependency evidence']}
