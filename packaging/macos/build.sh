#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
version=3.0.5
arch="$(uname -m)"
case "$arch" in
  arm64)
    triple=aarch64-apple-darwin
    pdfium_arch=arm64
    rhwp_arch=aarch64
    pdfium_sha=0d6781fe08906baff3d82c90953e519fbc4eb253fe76431e5ed53b157763b97c
    rhwp_sha=6ed46aa63f6d10c447842999f73abc59cf9586c5645fefc5e763bd6a6d43662d
    ;;
  x86_64)
    triple=x86_64-apple-darwin
    pdfium_arch=x64
    rhwp_arch=x86_64
    pdfium_sha=40865f34642c34d82cc336132df9e0347133f4692cd46776647af160f9a5cca9
    rhwp_sha=07b1a5ea570e410d1b52b4d848a94f00c64674e84004883c62efad7daab61da4
    ;;
  *) echo "Unsupported macOS architecture: $arch" >&2; exit 1 ;;
esac

base="$PWD"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
out="${1:-$base/dist/macos}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
target="${CARGO_TARGET_DIR:-$base/rust/magic-converter/target}"
app="$out/MagicConverter.app"
bundle="$app/Contents"
mkdir -p "$bundle/MacOS" "$bundle/Resources/Notices" "$bundle/vendor/pdfium" "$bundle/vendor/rhwp"
export MACOSX_DEPLOYMENT_TARGET=15.0

xcrun swiftc -swift-version 5 -O -target "$arch-apple-macosx15.0" packaging/macos/MagicConverter.swift -o "$bundle/MacOS/MagicConverter"
cargo test --locked --release --target "$triple" --target-dir "$target" --manifest-path rust/magic-converter/Cargo.toml --lib
cargo build --locked --release --target "$triple" --target-dir "$target" --manifest-path rust/magic-converter/Cargo.toml --bin magic-converter-rust --bin magic_converter_mcp
install -m 755 "$target/$triple/release/magic-converter-rust" "$bundle/MacOS/magic_converter-cli"
install -m 755 "$target/$triple/release/magic_converter_mcp" "$bundle/MacOS/magic_converter-mcp"
install -m 644 packaging/macos/Info.plist "$bundle/Info.plist"
install -m 644 LICENSE "$bundle/Resources/Notices/LICENSE-MagicConverter"

fetch() {
  curl --proto '=https' --tlsv1.2 -fLsS --retry 3 --output "$2" "$1"
  printf '%s  %s\n' "$3" "$2" | shasum -a 256 -c -
}
fetch "https://github.com/bblanchon/pdfium-binaries/releases/download/chromium/8076/pdfium-mac-$pdfium_arch.tgz" "$work/pdfium.tar.gz" "$pdfium_sha"
fetch "https://github.com/edwardkim/rhwp/releases/download/v0.8.7/rhwp-v0.8.7-macos-$rhwp_arch.tar.gz" "$work/rhwp.tar.gz" "$rhwp_sha"
mkdir "$work/pdfium" "$work/rhwp"
tar -xzf "$work/pdfium.tar.gz" -C "$work/pdfium"
tar -xzf "$work/rhwp.tar.gz" -C "$work/rhwp"
install -m 755 "$work/pdfium/lib/libpdfium.dylib" "$bundle/vendor/pdfium/libpdfium.dylib"
rhwp_bin="$(find "$work/rhwp" -type f -name rhwp -print)"
test -f "$rhwp_bin"
install -m 755 "$rhwp_bin" "$bundle/vendor/rhwp/rhwp"
mkdir "$bundle/Resources/Notices/PDFium" "$bundle/Resources/Notices/rhwp"
install -m 644 "$work/pdfium/LICENSE" "$bundle/Resources/Notices/PDFium/LICENSE"
cp -R "$work/pdfium/licenses" "$bundle/Resources/Notices/PDFium/"
find "$work/rhwp" -type f \( -iname '*license*' -o -iname '*notice*' \) \
  -exec cp {} "$bundle/Resources/Notices/rhwp/" \;
if [ -z "$(find "$bundle/Resources/Notices/rhwp" -type f -print -quit)" ]; then
  curl --proto '=https' --tlsv1.2 -fLsS --retry 3 \
    'https://raw.githubusercontent.com/edwardkim/rhwp/v0.8.7/LICENSE' \
    -o "$bundle/Resources/Notices/rhwp/LICENSE"
fi
install -m 644 vendor/rhwp/THIRD_PARTY_LICENSES.md "$bundle/Resources/Notices/rhwp/THIRD_PARTY_LICENSES.md"
cp docs/user/MACOS.md "$bundle/Resources/"

for executable in "$bundle"/MacOS/* "$bundle/vendor/rhwp/rhwp" "$bundle/vendor/pdfium/libpdfium.dylib"; do
  lipo "$executable" -verify_arch "$arch"
  codesign --force --sign - "$executable"
done
codesign --force --sign - "$app"
codesign --verify --deep --strict "$app"
plutil -lint "$bundle/Info.plist"
name="MagicConverter-$version-macOS-$arch.zip"
ditto -c -k --sequesterRsrc --keepParent "$app" "$out/$name"
(cd "$out" && shasum -a 256 "$name" > "$name.sha256")
