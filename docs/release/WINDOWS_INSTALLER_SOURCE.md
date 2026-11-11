# Windows installer source for 2.2.31

The [v2.2.31 release](https://github.com/handaeho/MagicConverter/releases/tag/v2.2.31)
provides the corresponding application and native sources, build inputs, notices
and SHA-256 checksums alongside the Windows ZIP containing both wizards.

On Windows x64, extract `MagicConverter-corresponding-source.zip` and use Python
3.14 and the Windows .NET Framework 4.x C# compiler:

```powershell
python .\tools\build\build_public_windows.py --output .\installer-output
python .\tools\build\build_windows_uninstaller.py --output .\uninstaller-output
Compress-Archive -LiteralPath .\installer-output\MagicConverter-Setup.exe, .\uninstaller-output\Uninstall.exe -DestinationPath .\MagicConverter-2.2.31-Windows-x64.zip
```

The first command obtains the pinned `MagicConverter-runtime-inputs.zip` and
`MagicConverter-native-engines.zip` from the release. For a local rebuild,
extract the runtime inputs into a folder and pass it as `--runtime-dir`.
Place the intact engine ZIP in another folder as
`<sha256>-MagicConverter-native-engines.zip` and pass that folder as
`--vendor-cache`. Each input is checked against the locks under
`packaging/public-windows`. The build manifest lists the hashes of the source
and embedded files. The timestamps and identities created by the C# compiler
can change the whole EXE hash on rebuild.

`MagicConverter-native-corresponding-source.zip` contains the Qt and PySide
sources, patch files and build recipe, the PyMuPDF and MuPDF sources, the FFmpeg
and static codec sources, the HEIC libraries, and the pinned rhwp materials.
See its `BUILD.md` for the commands and toolchain. The Microsoft runtime DLLs
are separate object code under their accompanying terms. Their redistribution
depends on the Visual Studio license and the
[Microsoft Distributable Code list](https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution).

Qt/PySide and libheif are dynamically linked. You may modify, replace and
debug those libraries as their licenses permit. Close MagicConverter before
replacing libraries in an installed copy. Qt DLLs are in
`runtime/Lib/site-packages/PySide6` alongside the bindings and the `plugins`
folder. Keep the replacement ABI and plugin versions compatible, and replace
the binding wheel files together if you rebuild PySide or Shiboken. HEIC DLLs
are in `runtime/Lib/site-packages`; a replacement must expose the same API
and use the same DLL names. The launcher has no signature or hash gate on
those installed files. To make a new installer with modified libraries, update
the appropriate wheel pins, supply a local runtime directory, and regenerate
the removal inventory from an installation of that build. The verification
hashes in Setup apply to its input archives during installation.
