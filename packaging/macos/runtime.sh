#!/usr/bin/env bash
set -euo pipefail
bundle="$1/Contents"
work="$2"
export HOMEBREW_NO_AUTO_UPDATE=1
brew install qtbase qtsvg ffmpeg-full libheif libavif pkgconf
qt="$(brew --prefix qtbase)"
svg="$(brew --prefix qtsvg)"
avif="$(brew --prefix libavif)"
heif="$(brew --prefix libheif)"
ffmpeg="$(brew --prefix ffmpeg-full)"
python3 -m venv "$work/build-python"
py="$work/build-python/bin/python"
"$py" -m pip install --disable-pip-version-check 'pymupdf==1.28.2'
mupdf="$("$py" -c 'import pymupdf; print(pymupdf.__path__[0])')"
export PKG_CONFIG_PATH="$qt/lib/pkgconfig:$svg/lib/pkgconfig:$avif/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
mkdir -p "$bundle/vendor/qt/plugins/platforms" "$bundle/vendor/heif" "$bundle/vendor/mupdf" "$bundle/vendor/ffmpeg"
clang++ -std=c++20 -O2 rust/magic-converter/native/qt_render.cpp \
  $(pkg-config --cflags --libs Qt6Gui Qt6Svg) -o "$bundle/vendor/qt/magic_qt_render"
cp "$qt/share/qt/plugins/platforms/libqoffscreen.dylib" "$bundle/vendor/qt/plugins/platforms/" || \
  cp "$qt/plugins/platforms/libqoffscreen.dylib" "$bundle/vendor/qt/plugins/platforms/"
printf '[Paths]\nPlugins=plugins\n' > "$bundle/vendor/qt/qt.conf"
clang -std=c11 -O2 -dynamiclib rust/magic-converter/native/avif_bridge.c \
  $(pkg-config --cflags --libs libavif) -o "$bundle/vendor/heif/libmagic_avif_bridge.dylib"
install -m 755 "$heif/lib/libheif.dylib" "$bundle/vendor/heif/libheif.dylib"
clang++ -std=c++20 -O2 -dynamiclib rust/magic-converter/native/mupdf_bridge.cpp \
  -I"$mupdf/mupdf-devel/include" -L"$mupdf" -lmupdfcpp \
  -o "$bundle/vendor/mupdf/libmagic_mupdf_bridge.dylib"
install -m 755 "$ffmpeg/bin/ffmpeg" "$bundle/vendor/ffmpeg/ffmpeg"
install -m 755 "$ffmpeg/bin/ffprobe" "$bundle/vendor/ffmpeg/ffprobe"
"$py" tools/build/bundle_macos_runtime.py --app "$1" \
  --search "$qt/lib" --search "$svg/lib" --search "$mupdf" \
  "$bundle/vendor/qt/magic_qt_render" "$bundle/vendor/qt/plugins/platforms/libqoffscreen.dylib" \
  "$bundle/vendor/heif/libheif.dylib" "$bundle/vendor/heif/libmagic_avif_bridge.dylib" \
  "$bundle/vendor/mupdf/libmagic_mupdf_bridge.dylib" \
  "$bundle/vendor/ffmpeg/ffmpeg" "$bundle/vendor/ffmpeg/ffprobe"
brew info --json=v2 --installed > "$bundle/Resources/Notices/Native/homebrew-build-inputs.json"
mkdir "$bundle/Resources/Notices/MuPDF"
find "$mupdf" -maxdepth 2 -type f \( -iname '*license*' -o -iname '*copying*' \) \
  -exec cp {} "$bundle/Resources/Notices/MuPDF/" \;
