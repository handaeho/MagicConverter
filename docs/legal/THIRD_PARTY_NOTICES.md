# Third-party components

## Windows installer 2.2.38

The installer embeds CPython 3.14.10, 34 pinned Python wheels, FFmpeg and
FFprobe 9.0.2 built from source, rhwp 0.8.7 and the app-local Microsoft VC runtime.
The engine license files and notices are installed under `app/vendor`.
CPython and wheel license files are installed with the runtime.
[Installer notices](../../packaging/public-windows/THIRD_PARTY_NOTICES.txt)

The [v2.2.38 release](https://github.com/handaeho/MagicConverter/releases/tag/v2.2.38)
includes the source archives, build inputs, patches and notices for FFmpeg and
its static codecs, Qt/PySide, PyMuPDF/MuPDF, HEIC and rhwp. The installer displays
the Microsoft runtime terms. See the [source and build instructions](../release/WINDOWS_INSTALLER_SOURCE.md)
for details and library replacement instructions.

## Earlier internal PyInstaller package

magic_converter bundles Python, PySide6/Qt, Pillow, FFmpeg, and FFprobe.
Version 1.4 also uses PyMuPDF/MuPDF, pypdfium2/PDFium, pypdf, pillow-heif/libheif,
python-pptx, python-docx, openpyxl, pdfplumber/pdfminer.six, Markdown,
Beautiful Soup, striprtf, xlrd, rarfile, pywin32 and olefile with their dependencies.
PyInstaller is used to create the executable. Components retain their own
licenses. The FFmpeg build's supplied LICENSE and README are included in the
one-file archive under vendor/ffmpeg.

- Python: https://docs.python.org/3/license.html
- PySide6 / Qt: https://doc.qt.io/qtforpython-6/licenses.html
- Pillow: https://github.com/python-pillow/Pillow/blob/main/LICENSE
- FFmpeg / FFprobe: https://ffmpeg.org/legal.html
- Included Windows FFmpeg build and source references: https://www.gyan.dev/ffmpeg/builds/
- FFmpeg source: https://ffmpeg.org/download.html
- PyInstaller: https://pyinstaller.org/en/stable/license.html
- pypdfium2 / PDFium: https://github.com/pypdfium2-team/pypdfium2/tree/main/LICENSES
- PyMuPDF / MuPDF: https://pymupdf.readthedocs.io/en/latest/about.html
- pypdf: https://github.com/py-pdf/pypdf/blob/main/LICENSE
- pillow-heif: https://github.com/bigcat88/pillow_heif
- python-pptx: https://github.com/scanny/python-pptx/blob/master/LICENSE
- python-docx: https://github.com/python-openxml/python-docx/blob/master/LICENSE
- openpyxl: https://openpyxl.readthedocs.io/en/stable/
- pdfplumber: https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt
- rarfile: https://github.com/markokr/rarfile/blob/master/LICENSE
- pywin32: https://github.com/mhammond/pywin32
- olefile: https://github.com/decalage2/olefile
- rhwp 0.8.7 (MIT), https://github.com/edwardkim/rhwp/releases/tag/v0.8.7.
  Official Windows x86_64 executable, upstream LICENSE and THIRD_PARTY_LICENSES.md
  are included under vendor/rhwp. Its Microsoft VC runtime 14.44.35208 DLLs are
  distributed app-local with their original runtime license files. Pinned asset
  URLs and SHA-256 values are documented in vendor/rhwp/BUILD.md.


Stage-7 dependencies: fontTools 4.67.0 (MIT, including LICENSE.external),
Brotli 1.2.0 (MIT), zopfli 0.4.3 (Apache-2.0), PyYAML 6.0.3 (MIT),
Tomli-W 1.2.0 (MIT), defusedxml 0.7.1 (PSF).
Original installed-wheel license texts, reviewed versions and SHA-256 values are
included under licenses/stage7. The build refuses missing/changed notices and
mismatched installed versions. New font files, calibre, Ghostscript, ODA,
MSG/MAPI engines and .NET are not distributed. Font fixtures are generated in
diagnostics. User font licensing/embedding restrictions remain in effect.

Build inputs for the earlier internal package: Gyan FFmpeg 9.0.2 full build, including its
original LICENSE and README.txt. The app does not modify the bundled engines.

Stage 9 records all 51 reviewed build/runtime wheels in packaging/python-wheels.json,
with exact archive hashes and upstream source-archive references. Original wheel
notices (153 files), distribution metadata and Python's full license are retained
under licenses/runtime and checked against packaging/notices-lock.json. Vendor
archives and selected member hashes are in packaging/vendor-lock.json. Tracked
Blank HWPX and Filee reference assets are in packaging/source-assets.json.
The SPDX 2.3 SBOM distinguishes observed bundle inputs from build dependencies.
It records upstream license declarations; licenseConcluded remains NOASSERTION.
It does not exhaustively enumerate native subcomponents inside wheels/engines.
Qt commercial license text in a wheel is not evidence of a commercial entitlement.
Project licensing, AGPL/commercial selection, Qt module/source/relink obligations,
FFmpeg corresponding sources remain subject to
review. See packaging/release-policy.json; internal build success is not public
redistribution approval. The checked SPDX schema is from spdx/spdx-spec v2.3;
its original license is retained in packaging/schemas/SPDX-LICENSE.
