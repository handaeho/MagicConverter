"""Review an immutable native release recipe as data; never execute upstream CI."""
import hashlib
import re
import tomllib

import yaml


RECIPE_FILES = ('Cargo.toml', 'rust-toolchain.toml', '.github/workflows/release-binary.yml')


def rhwp_recipe(contents):
    if any(name not in contents for name in RECIPE_FILES):
        raise ValueError('Missing immutable native recipe input')
    cargo = tomllib.loads(contents['Cargo.toml'].decode('utf-8'))
    toolchain = tomllib.loads(contents['rust-toolchain.toml'].decode('utf-8'))['toolchain']
    workflow = yaml.safe_load(contents['.github/workflows/release-binary.yml'])
    channel = toolchain.get('channel')
    if not isinstance(channel, str) or not re.fullmatch(r'\d+\.\d+\.\d+', channel):
        raise ValueError('Native toolchain is not an exact version')
    build = workflow['jobs']['build']
    windows = [row for row in build['strategy']['matrix']['include'] if row['target'] == 'x86_64-pc-windows-msvc']
    commands = [step['run'].strip() for step in build['steps'] if step.get('name') == 'Build release binary']
    expected = 'cargo build --release --bin rhwp --target ${{ matrix.target }}'
    if len(windows) != 1 or windows[0]['runner'] != 'windows-latest' or commands != [expected]:
        raise ValueError('Native release recipe differs; review new target/flags/runner')
    if cargo['package']['name'] != 'rhwp' or cargo['package']['version'] != '0.8.7':
        raise ValueError('Native recipe package identity differs')
    defaults = cargo.get('features', {}).get('default', [])
    if not isinstance(defaults, list) or any(not isinstance(v, str) for v in defaults):
        raise ValueError('Native default features invalid')
    return {'status': 'verified_declared_recipe', 'package': 'rhwp', 'version': '0.8.7',
            'toolchain_declared': channel, 'target': windows[0]['target'], 'runner_declared': windows[0]['runner'],
            'upstream_command': expected, 'default_features_declared': defaults,
            'review_rebuild_command': ['cargo', '+'+channel, 'build', '--offline', '--locked', '--release', '--bin', 'rhwp', '--target', windows[0]['target']],
            'upstream_locked_flag': False, 'upstream_features_flag': False,
            'recipe_inputs': [{'path': n, 'bytes': len(contents[n]), 'sha256': hashlib.sha256(contents[n]).hexdigest()} for n in RECIPE_FILES],
            'build_executed': False, 'binary_source_correspondence': False,
            'limitations': ['The stable toolchain action and moving Windows image do not attest the actual release compiler/SDK',
                            'Declared defaults are recipe evidence, not an attestation of shipped feature resolution',
                            'Offline/locked review command deliberately strengthens upstream input discipline; byte identity unproven']}
