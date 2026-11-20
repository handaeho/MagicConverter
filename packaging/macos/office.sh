#!/usr/bin/env bash
set -euo pipefail

# Keep the upstream standalone app unchanged, including its signatures and
# licenses. It is a private output engine, not a separate user installation.
app="$1"
work="$2"
version=26.8.3
case "$(uname -m)" in
  arm64)
    folder=aarch64
    architecture=aarch64
    sha=de943203795e5057785f65515063e2db5eb60b0bb6d8e2534fec59e5cc5fb988
    ;;
  x86_64)
    folder=x86_64
    architecture=x86-64
    sha=bb3c35d628780510915ad9e759d7ed898094d7985d01b2181bd1d4ecd94fd159
    ;;
  *) exit 1 ;;
esac
dmg="$work/LibreOffice.dmg"
url="https://download.documentfoundation.org/libreoffice/stable/$version/mac/$folder/LibreOffice_${version}_MacOS_${architecture}.dmg"
curl --proto '=https' --tlsv1.2 -fLsS --retry 3 -o "$dmg" "$url"
printf '%s  %s\n' "$sha" "$dmg" | shasum -a 256 -c -
mount="$work/office-mount"
mkdir -p "$mount" "$app/Contents/vendor/office" "$app/Contents/Resources/Notices/LibreOffice"
hdiutil attach -readonly -nobrowse -mountpoint "$mount" "$dmg"
trap 'hdiutil detach "$mount" >/dev/null || true' EXIT
ditto "$mount/LibreOffice.app" "$app/Contents/vendor/office/LibreOffice.app"
hdiutil detach "$mount"
trap - EXIT
office="$app/Contents/vendor/office/LibreOffice.app"
codesign --verify --deep --strict "$office"
"$office/Contents/MacOS/soffice" --headless --version
notices="$app/Contents/Resources/Notices/LibreOffice"
for name in LICENSE LICENSE.html LICENSE.fodt NOTICE CREDITS.html; do
  if [ -f "$office/Contents/Resources/$name" ]; then cp "$office/Contents/Resources/$name" "$notices/"; fi
done
python3 - "$office" "$notices" "$url" "$sha" <<'PY'
import json
import plistlib
import sys
from pathlib import Path
office, notices = map(Path, sys.argv[1:3])
info = plistlib.loads((office / 'Contents/Info.plist').read_bytes())
version = info['CFBundleShortVersionString']
source_version = '.'.join(version.split('.')[:3])
record = {'name': 'LibreOffice', 'version': version, 'binary_url': sys.argv[3],
          'sha256': sys.argv[4], 'modified': False,
          'source': f'https://download.documentfoundation.org/libreoffice/src/{source_version}/',
          'source_archive': f'https://download.documentfoundation.org/libreoffice/src/{source_version}/libreoffice-{version}.tar.xz',
          'license': 'https://www.libreoffice.org/licenses/'}
(notices / 'source-and-license.json').write_text(json.dumps(record, indent=2) + '\n')
PY
