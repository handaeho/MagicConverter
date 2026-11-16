# rhwp build assets

The portable EXE bundles the official, unmodified Windows x86_64 rhwp 0.8.7.
It does not download engines at runtime or require Hancom, Rust or Python.

Prepare the ignored executable/DLL build inputs with Python 3.14+:

```powershell
.\.venv\Scripts\python.exe tools\fetch_vendor.py
```

Pinned inputs (also pinned by Filee b249fec88463ad283750038ae3c317fa11ad50cb):

The common manifest `packaging/vendor-lock.json` now covers FFmpeg, rhwp,
the VC runtime and the separate upstream THIRD_PARTY_LICENSES.md. `build-python-reference.ps1`
acquires these inputs automatically; `fetch_rhwp.py` delegates to the same tool.
Archive and every selected member are checked before publication. `--offline`
uses the reviewed cache and `--verify` checks installed bytes without downloads.

| Component | URL | Archive SHA-256 |
| --- | --- | --- |
| rhwp 0.8.7 | https://github.com/edwardkim/rhwp/releases/download/v0.8.7/rhwp-v0.8.7-windows-x86_64.zip | f7ac326e98f595ae8fecd8da7a614ee9a37db77bdf938782a8e07c9da473ec6f |
| Microsoft VC runtime 14.44.35208 | https://conda.anaconda.org/conda-forge/win-64/vc14_runtime-14.44.35208-h818238b_41.conda | ec706fe9368b837fbd05af4851f5ebef12bef8c471a273f4805f7af788407317 |

`build.spec` requires rhwp.exe, msvcp140.dll, vcruntime140.dll,
vcruntime140_1.dll and the accompanying licenses. The runtime DLLs stay beside
rhwp.exe inside the extracted one-file bundle, so no system installation is needed.
The engine is located only in the bundled vendor directory, never through PATH.

rhwp's MIT license, README and upstream third-party notices are included here.
The upstream dependency notices are from the v0.8.7 tag. Microsoft runtime
licenses are retained under runtime-licenses with original filenames.

The renderer's timeout is 120 seconds plus 1,800 seconds per MiB of section XML,
capped at one hour and bounded by the job's own deadline. Process cancellation
uses the same owned Windows job containment as FFmpeg and Office helpers.
