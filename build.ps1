param(
    [Parameter(Mandatory)]
    [string] $HeifDirectory,
    [Parameter(Mandatory)]
    [string] $QtRuntimeDirectory,
    [Parameter(Mandatory)]
    [string] $FfmpegRuntimeDirectory,
    [string] $PdfiumLibrary = '.venv/Lib/site-packages/pypdfium2_raw/pdfium.dll',
    [string] $MuPdfDirectory = '.venv/Lib/site-packages/pymupdf',
    [string] $BuildDirectory = 'build/rust-windows-offline',
    [string] $PayloadDirectory,
    [string] $OutputDirectory,
    [string] $PublishedRuntimeAssets,
    [string] $Cargo = 'cargo.exe',
    [string] $CppCompiler = 'cl.exe',
    [string] $LibraryTool = 'lib.exe'
)

$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'rust/magic-converter/Cargo.toml') -Raw
if ($manifest -notmatch '(?m)^version = "([0-9]+\.[0-9]+\.[0-9]+)"') {
    throw 'Could not read the Rust product version.'
}
$version = $Matches[1]
if (-not $OutputDirectory) { $OutputDirectory = "dist/v$version" }
if (-not $PayloadDirectory) { $PayloadDirectory = Join-Path $BuildDirectory "payload-$version" }

$parameters = @{
    OutputDirectory = $OutputDirectory
    BuildDirectory = $BuildDirectory
    PayloadDirectory = $PayloadDirectory
    PublishedRuntimeAssets = $PublishedRuntimeAssets
    PdfiumLibrary = $PdfiumLibrary
    MuPdfDirectory = $MuPdfDirectory
    Cargo = $Cargo
    CppCompiler = $CppCompiler
    LibraryTool = $LibraryTool
    HeifDirectory = $HeifDirectory
    QtRuntimeDirectory = $QtRuntimeDirectory
    FfmpegRuntimeDirectory = $FfmpegRuntimeDirectory
}
& (Join-Path $PSScriptRoot 'tools/build/build_rust_windows_offline.ps1') @parameters

if (-not [IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $PSScriptRoot $OutputDirectory
}
$files = 'magic_converter-setup.exe', 'magic_converter-uninstall.exe'
$assets = $files | ForEach-Object { Join-Path $OutputDirectory $_ }
$zip = Join-Path $OutputDirectory "MagicConverter-$version-Windows-x64.zip"
Compress-Archive -LiteralPath $assets -DestinationPath $zip -CompressionLevel NoCompression
$checksums = foreach ($file in @($assets) + @($zip)) {
    '{0}  {1}' -f (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant(), (Split-Path $file -Leaf)
}
Set-Content -LiteralPath (Join-Path $OutputDirectory 'SHA256SUMS.txt') -Value $checksums -Encoding ascii
Write-Host "Built offline release: $zip"
