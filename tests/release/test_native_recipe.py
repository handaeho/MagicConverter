import unittest
from tools.build.native_recipe import rhwp_recipe


class NativeRecipeTests(unittest.TestCase):
    def inputs(self):
        return {'Cargo.toml': b'[package]\nname="rhwp"\nversion="0.8.7"\n[features]\ndefault=["console_error_panic_hook"]\n',
                'rust-toolchain.toml': b'[toolchain]\nchannel="1.93.1"\n',
                '.github/workflows/release-binary.yml': b'''jobs:
  build:
    strategy:
      matrix:
        include:
          - target: x86_64-pc-windows-msvc
            runner: windows-latest
    steps:
      - name: Build release binary
        run: cargo build --release --bin rhwp --target ${{ matrix.target }}
'''}

    def test_exact_recipe_records_declared_features_without_claiming_execution(self):
        report = rhwp_recipe(self.inputs())
        self.assertEqual(report['toolchain_declared'], '1.93.1')
        self.assertEqual(report['default_features_declared'], ['console_error_panic_hook'])
        self.assertFalse(report['build_executed'])
        self.assertFalse(report['binary_source_correspondence'])
        self.assertIn('--offline', report['review_rebuild_command'])

    def test_missing_floating_toolchain_changed_target_flags_and_identity_are_rejected(self):
        variants = [('rust-toolchain.toml', None), ('rust-toolchain.toml', b'[toolchain]\nchannel="stable"'),
                    ('Cargo.toml', self.inputs()['Cargo.toml'].replace(b'0.8.7', b'0.8.8')),
                    ('.github/workflows/release-binary.yml', self.inputs()['.github/workflows/release-binary.yml'].replace(b'--release', b'--release --features native-skia')),
                    ('.github/workflows/release-binary.yml', self.inputs()['.github/workflows/release-binary.yml'].replace(b'windows-msvc', b'windows-gnu'))]
        for name, value in variants:
            with self.subTest(name=name, value=value):
                inputs = self.inputs()
                if value is None: inputs.pop(name)
                else: inputs[name] = value
                with self.assertRaises(ValueError): rhwp_recipe(inputs)
