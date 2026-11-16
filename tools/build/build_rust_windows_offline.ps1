param(
    [Parameter(Mandatory)]
    [string] $OutputDirectory,
    [string] $Cargo = 'cargo.exe',
    [string] $BuildDirectory = 'build/rust-windows-offline',
    [string] $PayloadDirectory,
    [string] $PublishedRuntimeAssets,
    [string] $PdfiumLibrary = '.venv/Lib/site-packages/pypdfium2_raw/pdfium.dll',
    [string] $MuPdfDirectory = '.venv/Lib/site-packages/pymupdf',
    [string] $CppCompiler = 'cl.exe',
    [string] $LibraryTool = 'lib.exe',
    [Parameter(Mandatory)]
    [string] $HeifDirectory,
    [Parameter(Mandatory)]
    [string] $QtRuntimeDirectory,
    [Parameter(Mandatory)]
    [string] $FfmpegRuntimeDirectory
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$absolute = {
    param([string] $Path)
    if ([System.IO.Path]::IsPathRooted($Path)) { return [System.IO.Path]::GetFullPath($Path) }
    return [System.IO.Path]::GetFullPath((Join-Path $root $Path))
}
$build = & $absolute $BuildDirectory
$output = & $absolute $OutputDirectory
$pdfium = & $absolute $PdfiumLibrary
$mupdf = & $absolute $MuPdfDirectory
$heif = & $absolute $HeifDirectory
$qtRuntime = & $absolute $QtRuntimeDirectory
$ffmpegRuntime = & $absolute $FfmpegRuntimeDirectory
$ffmpegLockFile = Join-Path $PSScriptRoot 'rust_ffmpeg_runtime.json'
$ffmpegLock = Get-Content -LiteralPath $ffmpegLockFile -Raw | ConvertFrom-Json
$ffmpegFiles = foreach ($item in $ffmpegLock.files.PSObject.Properties) {
    $file = Join-Path $ffmpegRuntime $item.Name
    if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $item.Value) {
        throw "Supply the pinned FFmpeg full shared runtime from $($ffmpegLock.source). Missing or mismatched $file."
    }
    $file
}
$qtLockFile = Join-Path $PSScriptRoot 'rust_qt_runtime.json'
$qtLock = Get-Content -LiteralPath $qtLockFile -Raw | ConvertFrom-Json
foreach ($item in $qtLock.pyside_files.PSObject.Properties) {
    $file = Join-Path $qtRuntime $item.Name
    if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $item.Value) {
        throw "Supply the pinned Qt runtime from prepare_rust_qt.py. Missing or mismatched $file."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $qtRuntime 'magic_qt_render.exe') -PathType Leaf)) {
    throw 'Supply the compiled Qt helper from prepare_rust_qt.py.'
}
$heifLockFile = Join-Path $PSScriptRoot 'rust_heif_runtime.json'
$heifLock = Get-Content -LiteralPath $heifLockFile -Raw | ConvertFrom-Json
$heifFiles = foreach ($package in $heifLock.packages.PSObject.Properties) {
    foreach ($library in $package.Value.dlls.PSObject.Properties) {
        $file = Join-Path $heif $library.Name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $library.Value) {
            throw "Supply the pinned HEIF runtime from prepare_rust_heif.py. Missing or mismatched $file."
        }
        $file
    }
}
$avifHeader = Join-Path $heif 'include/avif/avif.h'
if (-not (Test-Path -LiteralPath $avifHeader -PathType Leaf) -or
    (Get-FileHash -LiteralPath $avifHeader -Algorithm SHA256).Hash -ne $heifLock.packages.avif.headers.'avif/avif.h') {
    throw 'Supply the pinned AVIF header from prepare_rust_heif.py.'
}
$pdfiumHash = '69F1E860F6742FBAAFCC4DB80E785EEC9A4EE0DA36EA6D1DC0867EA0F18BAEA6'
if (-not (Test-Path -LiteralPath $pdfium -PathType Leaf) -or (Get-FileHash -LiteralPath $pdfium -Algorithm SHA256).Hash -ne $pdfiumHash) {
    throw 'Supply the Windows x64 PDFium 156.0.8076.0 DLL from pypdfium2 5.14.0 using -PdfiumLibrary.'
}
if (Test-Path -LiteralPath $output) { throw 'Choose a new output directory.' }
$muPdfLibrary = Join-Path $mupdf 'mupdfcpp64.dll'
$muPdfHash = '998184DCE298F24A4C2B080A9173594C96C6F78796732BDD8E95CA0E557557CB'
if (-not (Test-Path -LiteralPath $muPdfLibrary -PathType Leaf) -or
    (Get-FileHash -LiteralPath $muPdfLibrary -Algorithm SHA256).Hash -ne $muPdfHash) {
    throw 'Supply the Windows x64 MuPDF DLL and development files from PyMuPDF 1.28.2 using -MuPdfDirectory.'
}
$rhwp = Join-Path $root 'vendor/rhwp'
$rhwpHash = '1E1B7AE6C9FFD895E1A330A191666A2629CC7EBA2FC9E37442E9F1755741BB62'
if (-not (Test-Path -LiteralPath (Join-Path $rhwp 'rhwp.exe') -PathType Leaf) -or
    (Get-FileHash -LiteralPath (Join-Path $rhwp 'rhwp.exe') -Algorithm SHA256).Hash -ne $rhwpHash) {
    throw 'Supply the repository-pinned rhwp 0.8.7 Windows x64 binary.'
}
$previousFlags = $env:RUSTFLAGS
$env:RUSTFLAGS = ("$previousFlags -C target-feature=+crt-static").Trim()

try {
    & $Cargo build --release --offline --locked --manifest-path (Join-Path $root 'rust/magic-converter/Cargo.toml') --target-dir (Join-Path $build 'app')
    if ($LASTEXITCODE -ne 0) { throw "Rust app build failed with exit $LASTEXITCODE." }
    & $Cargo build --release --offline --locked --manifest-path (Join-Path $root 'rust/windows-installer/Cargo.toml') --target-dir (Join-Path $build 'installer')
    if ($LASTEXITCODE -ne 0) { throw "Rust installer build failed with exit $LASTEXITCODE." }
} finally {
    $env:RUSTFLAGS = $previousFlags
}

$payload = if ($PayloadDirectory) { & $absolute $PayloadDirectory } else { Join-Path $build 'payload' }
if (Test-Path -LiteralPath $payload) { throw 'Choose a new payload directory.' }
$bridge = Join-Path $build 'mupdf-bridge'
New-Item -ItemType Directory -Force -Path $bridge | Out-Null
$muPdfInclude = Join-Path $mupdf 'mupdf-devel/include'
$muPdfImportLibrary = Join-Path $mupdf 'mupdf-devel/lib/mupdfcpp64.lib'
$muPdfSource = Join-Path $root 'rust/magic-converter/native/mupdf_bridge.cpp'
& $CppCompiler /nologo /LD /EHsc /std:c++17 /O2 /MD "/I$muPdfInclude" "/Fo$bridge/mupdf_bridge.obj" "/Fe$bridge/magic_mupdf_bridge.dll" $muPdfSource $muPdfImportLibrary /link "/IMPLIB:$bridge/magic_mupdf_bridge.lib"
if ($LASTEXITCODE -ne 0) { throw "MuPDF bridge build failed with exit $LASTEXITCODE." }
$avifInclude = Join-Path $heif 'include'
$avifImports = Join-Path $root 'rust/magic-converter/native/avif_imports.def'
$avifSource = Join-Path $root 'rust/magic-converter/native/avif_bridge.c'
& $LibraryTool /NOLOGO /MACHINE:X64 "/DEF:$avifImports" "/OUT:$bridge/avif.lib"
if ($LASTEXITCODE -ne 0) { throw "AVIF import library build failed with exit $LASTEXITCODE." }
& $CppCompiler /nologo /LD /std:c11 /O2 /MT "/I$avifInclude" "/Fo$bridge/avif_bridge.obj" "/Fe$bridge/magic_avif_bridge.dll" $avifSource "$bridge/avif.lib" /link "/IMPLIB:$bridge/magic_avif_bridge.lib"
if ($LASTEXITCODE -ne 0) { throw "AVIF bridge build failed with exit $LASTEXITCODE." }
New-Item -ItemType Directory -Force -Path $output, (Join-Path $payload 'vendor/ffmpeg'), (Join-Path $payload 'vendor/pdfium'), (Join-Path $payload 'vendor/heif'), (Join-Path $payload 'vendor/rhwp'), (Join-Path $payload 'vendor/mupdf'), (Join-Path $payload 'assets') | Out-Null
Copy-Item -LiteralPath (Join-Path $build 'app/release/magic-converter-rust.exe') -Destination (Join-Path $payload 'magic_converter-cli.exe')
Copy-Item -LiteralPath (Join-Path $build 'app/release/magic_converter.exe') -Destination (Join-Path $payload 'magic_converter.exe')
Copy-Item -LiteralPath (Join-Path $build 'app/release/magic_converter_mcp.exe') -Destination (Join-Path $payload 'magic_converter-mcp.exe')
Copy-Item -LiteralPath (Join-Path $root 'assets/magic_converter-tool.ico') -Destination (Join-Path $payload 'assets')
Copy-Item -LiteralPath $ffmpegFiles -Destination (Join-Path $payload 'vendor/ffmpeg')
Copy-Item -LiteralPath $ffmpegLockFile -Destination (Join-Path $payload 'vendor/ffmpeg')
Copy-Item -LiteralPath $pdfium -Destination (Join-Path $payload 'vendor/pdfium/pdfium.dll')
Copy-Item -LiteralPath (Join-Path $root 'licenses/runtime/pypdfium2-5.14.0') -Destination (Join-Path $payload 'vendor/pdfium/licenses') -Recurse
Copy-Item -LiteralPath $heifFiles -Destination (Join-Path $payload 'vendor/heif')
Copy-Item -LiteralPath (Join-Path $bridge 'magic_avif_bridge.dll') -Destination (Join-Path $payload 'vendor/heif')
Copy-Item -LiteralPath (Join-Path $heif 'licenses') -Destination (Join-Path $payload 'vendor/heif/licenses') -Recurse
Copy-Item -LiteralPath $heifLockFile -Destination (Join-Path $payload 'vendor/heif')
foreach ($file in 'rhwp.exe', 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE', 'THIRD_PARTY_LICENSES.md', 'BUILD.md', 'runtime-licenses') {
    Copy-Item -LiteralPath (Join-Path $rhwp $file) -Destination (Join-Path $payload 'vendor/rhwp') -Recurse
}
Copy-Item -LiteralPath (Join-Path $root 'licenses/runtime/rust-workbook-fonts') -Destination (Join-Path $payload 'assets/font-licenses') -Recurse
Copy-Item -LiteralPath $muPdfLibrary, (Join-Path $bridge 'magic_mupdf_bridge.dll') -Destination (Join-Path $payload 'vendor/mupdf')
foreach ($file in 'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') {
    Copy-Item -LiteralPath (Join-Path $rhwp $file) -Destination (Join-Path $payload 'vendor/mupdf')
}
Copy-Item -LiteralPath (Join-Path $root 'licenses/runtime/pymupdf-1.28.2') -Destination (Join-Path $payload 'vendor/mupdf/licenses') -Recurse
Copy-Item -LiteralPath (Join-Path $root 'licenses/runtime/python-pptx-1.0.2/python_pptx-1.0.2.dist-info/LICENSE') -Destination (Join-Path $payload 'assets/presentation-template-LICENSE')
Copy-Item -LiteralPath (Join-Path $root 'rust/magic-converter/assets/document-template-LICENSE.txt') -Destination (Join-Path $payload 'assets')
Copy-Item -LiteralPath $qtRuntime -Destination (Join-Path $payload 'vendor/qt') -Recurse
Copy-Item -LiteralPath $qtLockFile -Destination (Join-Path $payload 'vendor/qt')
Copy-Item -LiteralPath (Join-Path $root 'licenses/runtime/rust-qt-6.12.0') -Destination (Join-Path $payload 'vendor/qt/licenses') -Recurse
Copy-Item -LiteralPath (Join-Path $root 'LICENSE'), (Join-Path $root 'licenses/Microsoft-VC-Runtime-2026.txt') -Destination $payload
if ($PublishedRuntimeAssets) {
    & (Join-Path $root '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'prepare_rust_release_runtime.py') --payload $payload --assets (& $absolute $PublishedRuntimeAssets)
    if ($LASTEXITCODE -ne 0) { throw "Published runtime preparation failed with exit $LASTEXITCODE." }
}
$installer = Join-Path $build 'installer/release/magic-converter-windows-setup.exe'
$setup = Join-Path $output 'magic_converter-setup.exe'
# Wait explicitly because PowerShell can launch a GUI executable asynchronously.
$startInfo = [Diagnostics.ProcessStartInfo]::new($installer)
$startInfo.Arguments = 'bundle --payload "{0}" --output "{1}"' -f $payload.TrimEnd('\'), $setup
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = [Diagnostics.Process]::Start($startInfo)
try {
    $standardOutput = $process.StandardOutput.ReadToEndAsync()
    $standardError = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $bundleReport = $standardOutput.GetAwaiter().GetResult()
    $diagnostic = $standardError.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $setup -PathType Leaf)) {
        throw "Rust bundle creation failed with exit $($process.ExitCode). $diagnostic $bundleReport"
    }
    $bundleReport | Write-Output
} finally {
    $process.Dispose()
}
Copy-Item -LiteralPath $installer -Destination (Join-Path $output 'magic_converter-uninstall.exe')
