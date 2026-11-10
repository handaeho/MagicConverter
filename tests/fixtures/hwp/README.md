
The HWP input referenced by `manifest.json` is the existing project-generated
one-cell table and orange picture fixture. The checked-in HWPX adds authored
Gulim/Batang runs, a middle dot, a Roman numeral, mixed sizes and asymmetric
36/48/60/72 pt margins. It contains no user document data or font binaries.
The writer uses the bundled Apache-2.0 blank template; its original LICENSE,
NOTICE and source are retained under `magic_converter/data/hwpx/`.

These always-run tests verify pinned bytes, declarations, content, object
counts and picture-preserving HWPX/DOCX round trips. They do not certify actual
font selection, visual layout or the approximately 12 pt native PDF offset.
The separate optional user-document regression remains optional and reports
its own skip when those original local files are absent.

`script-faces.hwpx` adds an independent mixed-script declaration control:
Arial for Latin, Gulim for Hangul, Batang for Hanja and Meiryo for Japanese.
It carries font names only. The middle dot and Roman numeral retain the
default face in the model test; actual renderer selection remains separate.
