# magic_converter 2.1.7: offline format support

| Family | Input | Output |
| --- | --- | --- |
| Images | JPG/JPEG, PNG, WebP, HEIC/HEIF, TIFF/TIF, AVIF, BMP, SVG | JPG, PNG, WebP, HEIC, TIFF, AVIF, BMP, PDF |
| Audio | MP3, M4A, WAV, FLAC, OGG, Opus, AIFF/AIF, WMA | MP3, M4A, WAV, FLAC, OGG, Opus, AIFF, WMA |
| Video | MP4, MOV, MKV, WebM, AVI, WMV, GIF | MP4, MOV, MKV, WebM, AVI, WMV, GIF; MP3 extraction from audio tracks |
| GIF image routes | GIF | Animated PNG/APNG and lossless WebP, with canvas, alpha, timing and repeat semantics |
| PDF | PDF | DOCX, PPTX (fixed), PPTX (editable), XLSX, JPG, PNG, EPUB, TXT, MD |
| Documents | DOCX, Markdown, TXT, EPUB, RTF, RTFD, textClipping | DOCX, Markdown, TXT, EPUB, HTML, PDF |
| HWP/HWPX | HWP / HWPX | DOCX, PDF, TXT; HWP → HWPX; HWPX → HWP |
| Office to HWP/HWPX | DOCX; PPTX / XLSX | DOCX → HWP / HWPX; PPTX / XLSX → HWP (text and tables) |
| Slides | PPT, PPTX | PDF, JPG, PNG (PowerPoint or LibreOffice required) |
| Spreadsheets | XLSX, ODS, CSV, TSV, XLS | XLSX, ODS, CSV, TSV, PDF |
| Subtitles | SRT, VTT | SRT, VTT, TXT |
| Archives | ZIP, TAR, GZIP/tar.gz, RAR | ZIP, TAR, tar.gz |
| Structured data | JSON, JSONL/NDJSON, YAML/YML, TOML, XML, CSV, TSV | Same family, subject to type/table constraints |
| Fonts | TTF, OTF, WOFF, WOFF2 | Same family; CFF/CFF2 to TTF refused |
| E-books | EPUB, FB2; TXT/MD/DOCX imports | EPUB, FB2, HTML, TXT, MD, DOCX; PDF/images via DOCX |
| Comics | CBZ; PDF | CBZ to PDF/image-centered EPUB; PDF to 144 DPI CBZ |
| Mail/web archives | EML, MHTML/MHT | HTML, TXT; EML archive ZIP with original/attachments/manifest |
| Triangle meshes | OBJ, STL, PLY | OBJ, STL, PLY, geometry only |
| WGS84 geospatial | GeoJSON, KML, KMZ, GPX | Same family; GPX only points/lines/track segments |

Stage 7 preservation/loss limits, packaging conditions and excluded formats are
specified in [docs/reference/FORMAT_CONTRACTS.md](../reference/FORMAT_CONTRACTS.md).
XML uses a typed container or the ordered `$xml` document model; it does not
guess an application-specific XML schema. Fonts retain their outline type and
original licensing flags. MSG/MOBI/EOT/CAD/FBX/Shapefile inputs are not registered.

TOML requires an object at the root. JSONL, CSV and TSV read as arrays, so
direct conversion between TOML and those three formats is unavailable.

SVG, PPT, XLS, RAR, RTF, RTFD and textClipping are input formats only.
No file is renamed to pretend to be a different format.

## PDF and slide output

- Every PDF page and exported slide is rendered at **300 DPI** for JPG/PNG.
  A new numbered folder contains `page_0001.png`, `page_0002.png`, etc.
- **PPTX (fixed)** (`pptx_image`) uses one full-page image at **200 DPI** on
  each slide. The result is `original_slide.pptx`.
- **PPTX (editable)** (`pptx_editable`) reconstructs individual objects using
  only the PDF. Rectangles, ellipses and straight lines become native shapes;
  other supported paths become editable freeforms with cubic Bezier curves.
  Fill, stroke, opacity, line width, caps, joins and supported dash patterns
  are retained. Text becomes transparent, movable text boxes matching each span's
  baseline, character advances, font size, RGB color, opacity, bold and italic
  flags. Original lines do not wrap automatically. PostScript font names are
  resolved against installed font metadata, including Windows per-user fonts.
  Geometric word gaps are restored as spaces. The result is
  `original_editable.pptx`. Detected ruled tables become native PowerPoint
  tables with editable cells, row/column spans, shading and borders when
  supported by actual rules. Repeated full-width row rules and continuous
  internal columns can also recover tables with omitted outside borders,
  including the header and label column. Missing borders remain missing.
  Filled layout panels are
  not treated as tables. Original grid operators are replaced, so moving the
  native table leaves no duplicate rules. Pictures become separate, movable
  picture shapes, including pictures within cells. Objects follow the PDF's
  paint order. Clipped paths, unsupported compound fills, shading, masks and
  transparency groups are replayed through the original PDF renderer into
  individual transparent picture regions at up to **200 DPI**. When extracted
  paint order cannot be matched to renderer calls, that page is preserved as
  one picture with an editing-limit notice. Already-rasterized
  content remains raster content; OCR and semantic diagram recognition are
  not included. Original PPTX grouping, object names and SmartArt are not recovered.
- Editable PPTX also recognises common **vector Cartesian charts** from regular
  categories, linear numeric ticks and matching marks. Supported cases include
  vertical/horizontal bars, grouped bars, multiple line series and bar/line
  combinations with a secondary value axis. Negative numbers, percentages,
  displayed decimal precision, category names and point colours are retained.
  Actual DrawingML charts include embedded XLSX workbooks for **Edit Data**.
  Visible numeric labels supply values; unlabelled marks use geometric estimates.
  Estimates generate a conversion notice and are identified individually in the
  workbook's **PDF Recovery** sheet. Labels absent from the PDF remain hidden.
  Hidden data, original formulas and unprinted numeric precision are unavailable.
  Native Office chart layout can differ slightly from PDF lettering/spacing.
  Raster/pie/doughnut/3D/stacked/logarithmic charts and ambiguous/clipped/shared
  drawing operators retain the ordinary PDF objects/pictures rather than guessed
  chart data. Recognition has no file-name, page-number or customer-specific rules.
- Both PPTX modes use PyMuPDF and python-pptx, with no Poppler or Office
  installation. Original PDFs are opened read-only and their streams are
  never rewritten. Existing output files receive `(1)`, `(2)`, etc.
- PDF/PPTX coordinates map 1:1 in points for equal-size pages. Because a deck
  has one canvas size, mixed-size pages fit the first page's canvas with
  proportional scaling and white margins. Scanned image text cannot become
  editable without OCR. Text with unavailable fonts/glyphs or unsupported
  vertical, skewed or right-to-left positioning remains in separate picture
  regions with an explicit editing-limit notice. Original clipping and
  overlapping paint order are retained. Fonts are not embedded
  in the generated PPTX: recipients need the same fonts, or must embed
  licensed full fonts using PowerPoint before sharing for further editing.
- PDF -> DOCX reconstructs styled paragraphs, native merged Word tables and
  displayed images in reading order, preserving each page's size. Borderless
  tables, nested tables, complex columns and floating objects may not be
  reconstructed exactly. Scanned pages remain images with an OCR notice;
  they are not presented as editable text or cells. Neither this path nor
  editable PPTX requires Word, PowerPoint, Poppler or additional downloads.
- PDF -> XLSX extracts ruled tables into cells, or page text into rows if no
  table is found. Each PDF page has its own worksheet.
- TXT/MD/EPUB use embedded text. OCR is not included; image-only PDFs produce
  a clear error for text/table extraction rather than empty output.
- PDF metadata removal clears document information and XMP, not visible text
  or other content embedded in the PDF.

## Local Office integration

Bundled **rhwp 0.8.7** converts HWP ↔ HWPX and renders PDF.
DOCX/PPTX/XLSX can also be saved as binary HWP via the document model,
HWPX writer and rhwp. System and user fonts are available to the renderer.
The Python HWP record reader remains a fallback for model import when rhwp fails.
No output format is implemented by merely changing a filename extension.

The document model retains paragraphs, nested/merged tables, embedded pictures,
headers/footers, and equations. Common fractions, radicals and sub/superscripts
are exchanged as editable Office Math/HWP equation objects. Original DOCX OMML
is retained for DOCX re-export. Unsupported cross-format math is linearized with
an explicit warning. Original HWP object counts are also checked independently
against binary table/picture/equation records. Reopened results are compared for
table spans, picture hashes, equation scripts and text; differences or incomplete
audits are retained in job history. PDF page validation does not establish visual
identity: font substitution, floating objects and pagination need visual review.
Complex-script PDF text is routed away from rhwp's unshaped renderer.

Additional Office input formats: **DOCM/DOTX/DOTM**, **PPTM/POTX/POTM/PPSX/PPSM**,
and **XLSM/XLTX/XLTM**. These are normalized as valid, macro-free standard OOXML
packages. VBA, ActiveX and embedded executable objects are removed, never run.
Word variants can output DOCX, slide variants PPTX, and workbook variants XLSX;
HWP/HWPX and PDF routes also apply. Standard XLSX output retains formulas,
merged cells and embedded pictures through openpyxl. Slide-to-document layout,
charts/animations and workbook print layout are not reproduced. Workbook formulas
are represented as strings in the document model. The detailed conversion slots
do not expose HWP → PPTX/XLSX.

Word, PowerPoint and Excel PDF export use pywin32 in an initialized COM
apartment, with dedicated automation sessions and finally cleanup. COM work
runs in a hidden helper process, with a 180-second timeout and cleanup scoped
to newly created, identity-checked server processes. Existing user documents
are preserved.

Office conversions have a separate single-worker queue, leaving normal text,
image and media work available. Helper stdout/stderr do not use inherited
pipes that could stall timeout cleanup. Windows job objects contain helper
children, and COM process cleanup remains limited to recorded identities.
Stage limits are 30 seconds to open, 90 to save, 15 to clean up and 180 total.
The tray's **진행/대기 변환 모두 취소** cancels queued work and terminates the
app-owned running helper; partial staging files are not published.

PDF rendering, PDF -> PPTX, text-oriented document conversions and spreadsheet
data conversions are built into the executable. PPT/PPTX -> PDF/JPG/PNG
requires an installed desktop PowerPoint or LibreOffice. The program uses the
local application; no document is uploaded. Existing user presentations are
not closed or modified.

LibreOffice can be discovered in its standard Windows installation directory,
PATH, `MAGIC_CONVERTER_LIBREOFFICE`, or `vendor/libreoffice/program/soffice.exe`.
The executable does not include or install LibreOffice or Microsoft Office.

## Fidelity and special cases

- Non-native document/EPUB/Markdown/RTF interchange is text-oriented. Embedded
  images and complex Word layout are not preserved through every format.
  DOCX -> PDF uses installed Word/LibreOffice when available for fidelity;
  otherwise a basic local text/table PDF renderer is used.
- Spreadsheet interchange preserves workbook sheet data, not all styles,
  charts, macros, external links or formula calculations. Multiple worksheets
  exported to CSV/TSV are all retained as separate files in a new folder.
  PDF output uses a local table renderer.
- RTFD reads the main RTF text, including when supplied as a ZIP bundle. Its
  attachments are not reconstructed. textClipping supports text in a plist;
  resource-fork-only macOS clippings must first be saved as TXT or RTF.
- Subtitle timing and cue text are preserved. WebVTT positioning and styling
  are not preserved through SRT/TXT.
- RAR uses an available local unrar/7-Zip/Windows bsdtar backend. Windows tar
  does not handle every encrypted, solid or split RAR; a clear error is shown
  when the available engine cannot decode a member. RAR output is unavailable.
- Archive conversion streams members into a new archive, never extracting
  archive-controlled paths onto disk. Traversal paths and links are rejected.
- GZIP archive output is tar.gz so multiple input members remain together.
- HEIC/AVIF are re-encoded via bundled codecs; HDR/color profiles and metadata
  are not guaranteed to survive conversion. The app currently normalizes
  images to 8-bit RGBA/RGB pixels.

All outputs use collision-safe names next to their source. Existing files and
folders are not replaced. Original source documents remain unchanged.

## Registered conversion routes

The format registry normalizes aliases such as JPEG/JPG, HEIF/HEIC, AIF/AIFF,
Markdown/MD and compound TAR.GZ/TGZ extensions. Requests resolve through the
available registered engines with a maximum of three conversion steps.
The existing single-family wheel layouts and action keys remain supported.
Mixed-family selections show common actions only when every input has a route;
for example, a PNG and a DOCX can each be exported to PDF in one batch.
This does not merge those outputs into one PDF.

Slide images now run through explicit slide -> PDF -> image routes. The request
API can likewise plan document or HWP/HWPX -> PDF -> page images. Intermediate
files are private and removed after success, failure or cancellation. Page image
folders cannot be intermediate inputs, and the two PDF -> PPTX modes are direct
PDF operations only. Unknown inputs and missing required engines are refused
before a batch starts; no unsupported member is silently omitted.

## File scheduling and cancellation

Each ordinary input has an independent queued/running/done/failed/cancelled/
timed-out result. Missing or damaged files do not abort other files whose
formats and routes passed request validation. Batch results keep input order.
One-archive ZIP and terminal actions remain combined operations.

The scheduler reserves each route's engine capacities before starting it;
general work (two workers) and Office work (one worker) remain separate.
The tray's job manager supports cancelling one file, one job or all work.
Cancelled queued files never start, and successful outputs already published
remain available when the rest of a batch is cancelled.

Per-file processing defaults to one hour, excluding queue wait. Python work
checks cancellation/deadlines between chunks, frames, rows, pages and before
publication; an active native library call finishes before its next check.
External processes are supervised separately: FFprobe 30 seconds, Office and
LibreOffice 180 seconds, FFmpeg 600 seconds without output. A file deadline
also stops its supervised child. Windows job objects contain children from
before their first instruction; termination waits for owned children and
retries transient log-file locks. Captured logs are bounded in memory and on disk.

## Recipes, wheel profiles and output policies

Recipes name an existing action and supported encoder options. Image actions
support quality 1-100 and a long edge of 0 (unchanged) or up to 16384 pixels,
without upscaling. H.264/VP9 actions support CRF; lossy audio actions support
32-512 kbps. Defaults retain each existing encoder's settings. Unsupported
option/action combinations are rejected, never silently applied to another action.

Shift and Ctrl can each use the existing automatic wheel or a named profile
with 1-12 ordered recipe IDs. Unsupported inputs remain disabled with reasons.
The displayed profile and immutable recipe/output snapshot survive settings
changes during the drag. Changed drop payloads are refused. Accepted requests
retain their options/output policy throughout queue wait and execution.

Outputs can use the source folder or a configured absolute folder. Templates
accept only {stem}, {tag} and {preset}; suffixes are chosen by the converter.
Existing files are preserved by numbering or reporting a collision failure.
Configured single-hop and multi-hop work first encodes privately, then publishes
the final file/folder atomically through cancellation-aware copies. Combined ZIP
uses the same output rule. Copy-path and terminal actions produce no output file.

Versioned settings migrate legacy QSettings once, use atomic JSON publication
and retain the previous valid backup. Corrupt primaries are preserved and
recovered from backup, or defaults if no backup is usable. Future schemas are
preserved. Import/reset/restore are drafts until Save. Local history retains up
to 1000 jobs (default 200; 0 disables new records), with file states, output paths,
errors, warnings and the recipe/options actually submitted. Its JSON is bounded
to 12 MiB; oldest jobs, including an oversized single job, are discarded to fit.
Clearing history never deletes converted files. Settings export excludes history.

## Tools (stage 4)

The automatic Ctrl wheel appends tools applicable to every selected input;
existing Shift conversion layouts are retained. Each tool has a direct route
with its own intent and cannot be an intermediate conversion. User recipes
store validated ToolOptions in settings schema 2; schema 1 migrates without
changing existing IDs, profiles or encoder options. Older apps preserve future
schemas instead of overwriting them. History also records submitted tool options.

- PDF merge combines PDFs and every image frame in selection order as one
  atomic job. PDF selection preserves requested order (including descending
  ranges) and drops duplicate page references. Split writes one PDF per selected
  page; rotate preserves the whole document and rotates only selected pages.
  Lossless compression retains page objects, compresses streams/deduplicates
  objects, and copies the original if the result would be larger. Encrypted PDFs
  and out-of-range page selections are refused.
- Image rotate/flip/grayscale/crop write PNG/APNG with full-frame pixels and
  alpha. APNG posters, fractional frame delays and identical TIFF frames remain
  distinct. Transformations are limited to 128 million total frame pixels.
  Frame extraction decodes sequentially and writes PNGs plus a timing manifest.
- Media trim re-encodes in the same format; mute creates video-only H.264 MP4.
  PNG frame extraction supports an accurate start, duration, every Nth frame and
  a maximum count. The default maximum is 10,000 (configurable to 100,000); an
  extra frame detects overflow and rolls the entire folder back. FFprobe checks
  output structure, while self-tests additionally decode real media completely.
- SRT/VTT parsing validates minutes, seconds, start/end order and nonempty text.
  VTT cue identifiers, settings, header blocks and multiline text survive VTT
  conversion/shifting, including inline timestamp shifts. Negative starts clamp
  to zero; cues ending before zero are omitted with warnings. Conversion to SRT
  warns when VTT positioning cannot be represented. Inputs are limited to 64 MiB
  and 100,000 cues. UTF-8/UTF-16 and legacy Korean decoding remain available.
- ZIP/TAR/GZIP extraction writes only into a private new folder and publishes
  atomically, including empty archives. Links/devices, encrypted ZIP entries,
  unsafe/Windows-reserved names and case/duplicate path collisions are refused.
  Counts and declared/actual expanded sizes are checked (100,000 entries, 4 GiB),
  with 64 MiB disk reserve. RAR remains an input for existing repacking only;
  its legacy external streaming backend is not used by the new extraction tool.
