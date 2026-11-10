# Third-party components

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
  unmodified for optional per-user registration after explicit settings consent.
- The blank HWPX template comes from python-hwpx (Apache-2.0). Its pinned
  source, LICENSE and NOTICE are bundled under magic_converter/data/hwpx.
  Only the template is used; python-hwpx is not a runtime dependency.
- Filee (MIT), Copyright (c) 2026 Myeonggu Kim (KnifeLemon) and Filee
  contributors: https://github.com/KnifeLemon/Filee
  The converter contract, format/engine catalog separation and bounded graph
  routing design were referenced at commit
  b249fec88463ad283750038ae3c317fa11ad50cb, especially
  src/Filee.Core/Conversion/IConverter.cs, RoutePlanner.cs and ConverterCatalog.cs.
  Stage 3 also references the separation of immutable conversion recipes,
  stable profile references, output rules and per-file history in
  src/Filee.Core/Presets/Preset.cs, Profiles/ToolbarProfile.cs,
  Settings/AppSettings.cs and History/HistoryEntry.cs at the same commit.
  Stage 4 references PDF page/merge/split options, image/frame operations and
  archive/subtitle validation from Presets/Preset.cs and the Pdf, Magick, Media,
  Subtitles and Archives directories under src/Filee.Engines. These tools are
  implemented in Python with the already bundled PDF, Pillow and FFmpeg engines.
  Stage 5 references Hwp/RhwpConverter.cs and the HWPX document model, especially
  Hwp/Hwpx/Docx/OmmlReader.cs, EquationScript.cs and HwpxWriter.Objects.cs.
  Common equation box proportions are adapted from EquationScript.Measure;
  original OMML preservation, Python model and loss audit are implemented locally.
  Stage 6 references Core/Watching/FolderWatcher.cs, WatchRule.cs and
  GeneratedWatchOutputs.cs; Core/Web/WebDrop.cs and WebDownloader.cs;
  App/Services/SingleInstance.cs and CommandLine.cs; Filee.Cli/CliOptions.cs;
  and the Windows Explorer integration sources and their tests. Stable-input
  settling, generated-output exclusions, bounded web transfers and per-profile
  IPC forwarding informed the Python implementation. Watch result receipts,
  SQLite journals and current-user Qt IPC are implemented locally. The Explorer
  entry uses the classic per-user menu; Filee's Windows 11 COM/MSIX package is
  not included or installed.
  Stage 7 references Data/DataConverter.cs, XmlData.cs and CsvData.cs;
  Fonts/FontConverter.cs and FontFile.cs; Ebooks/Book.cs, EbookConverter.cs and
  Fb2Writer.cs; Email/EmlConverter.cs; Models3D/ModelConverter.cs and ObjFormat.cs;
  Geo/GeoConverter.cs and docs/ENGINES.md. Model boundaries, direct conversion
  paths and preservation notices informed the Python implementation.
  docs/reference/FORMAT_CONTRACTS.md specifies its shipped scope and exclusions.
  The Python implementation uses the existing magic_converter encoders; Filee
  and its .NET runtime/engines are not runtime dependencies.
  The upstream copyright and permission notice is included in licenses/Filee-MIT.txt.

Stage-7 dependencies: fontTools 4.67.0 (MIT, including LICENSE.external),
Brotli 1.2.0 (MIT), zopfli 0.4.3 (Apache-2.0), PyYAML 6.0.3 (MIT),
Tomli-W 1.2.0 (MIT), defusedxml 0.7.1 (PSF).
Original installed-wheel license texts, reviewed versions and SHA-256 values are
included under licenses/stage7. The build refuses missing/changed notices and
mismatched installed versions. New font files, calibre, Ghostscript, ODA,
MSG/MAPI engines and .NET are not distributed. Font fixtures are generated in
diagnostics. User font licensing/embedding restrictions remain in effect.

Build inputs for this release: Gyan FFmpeg 9.0.2 full build, including its
original LICENSE and README.txt. The app does not modify the bundled engines.

Stage 9 records all 51 reviewed build/runtime wheels in packaging/python-wheels.json,
with exact archive hashes and upstream source-archive references. Original wheel
notices (153 files), distribution metadata and Python's full license are retained
under licenses/runtime and checked against packaging/notices-lock.json. Vendor
archives and selected member hashes are in packaging/vendor-lock.json. Tracked
The SPDX 2.3 SBOM distinguishes observed bundle inputs from build dependencies.
It records upstream license declarations; licenseConcluded remains NOASSERTION.
It does not exhaustively enumerate native subcomponents inside wheels/engines.
Qt commercial license text in a wheel is not evidence of a commercial entitlement.
Project licensing, AGPL/commercial selection, Qt module/source/relink obligations,
review. See packaging/release-policy.json; internal build success is not public
redistribution approval. The checked SPDX schema is from spdx/spdx-spec v2.3;
its original license is retained in packaging/schemas/SPDX-LICENSE.
