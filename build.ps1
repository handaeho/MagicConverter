param(
    [switch]$SkipInstall,
    [switch]$Offline,
    [switch]$PublicRelease,
    [string]$BuildPython,
    [string]$Wheelhouse,
    [string]$VendorCache,
    [string]$WorkPath = 'build\release'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($PublicRelease) {
    $releasePolicy = Get-Content -LiteralPath 'packaging\release-policy.json' -Raw | ConvertFrom-Json
    if ($releasePolicy.public_windows_executables -isnot [bool] -or -not $releasePolicy.public_windows_executables) {
        throw 'Public Windows executable builds are excluded by packaging/release-policy.json.'
    }
}
if (-not $BuildPython) { $BuildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not $Wheelhouse) { $Wheelhouse = Join-Path $PSScriptRoot '.build-cache\wheels' }
if (-not $VendorCache) { $VendorCache = Join-Path $PSScriptRoot '.build-cache\vendor' }
if (-not (Test-Path -LiteralPath $BuildPython)) {
    & py -3.14 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv (Windows x64 Python 3.14.7 required).' }
    if (-not (Test-Path -LiteralPath $BuildPython)) { throw 'Requested BuildPython does not exist.' }
}
$config = Get-Content -LiteralPath 'packaging\build-environment.json' -Raw | ConvertFrom-Json
$actualPython = & $BuildPython -c "import platform,sys; print(platform.python_version() + '/' + platform.machine() + '/' + sys.platform)"
if ($LASTEXITCODE -ne 0 -or $actualPython -ne "$($config.python)/AMD64/win32") { throw 'Build requires Windows x64 Python 3.14.7.' }
$env:SOURCE_DATE_EPOCH = [string]$config.source_date_epoch
$env:PYTHONHASHSEED = [string]$config.pythonhashseed
$env:PYTHONUTF8 = '1'
$env:MAGIC_BUILD_WHEELHOUSE = [System.IO.Path]::GetFullPath($Wheelhouse)
New-Item -ItemType Directory -Force -Path $Wheelhouse | Out-Null
if (-not $SkipInstall) {
    if (-not $Offline) {
        & $BuildPython -m pip download --no-deps --require-hashes --only-binary=:all: -r requirements\windows-py314.lock --dest $Wheelhouse
        if ($LASTEXITCODE -ne 0) { throw 'Locked wheel acquisition failed.' }
    }
    & $BuildPython -m pip install --no-index --find-links $Wheelhouse --require-hashes --only-binary=:all: -r requirements\windows-py314.lock
    if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
}
& $BuildPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Build dependency closure is inconsistent.' }
$vendorArgs = @('tools\dependencies\fetch_vendor.py', '--cache', $VendorCache)
if ($Offline) { $vendorArgs += '--offline' }
& $BuildPython @vendorArgs
if ($LASTEXITCODE -ne 0) { throw 'Locked vendor acquisition failed.' }
& $BuildPython tools\build\build_inventory.py
if ($LASTEXITCODE -ne 0) { throw 'Reviewed inputs/notice gate failed.' }
if ($PublicRelease) {
    & $BuildPython tools\build\build_inventory.py --public
    if ($LASTEXITCODE -ne 0) { throw 'Public release policy evidence is incomplete.' }
}
& $BuildPython tools\build\generate_icon.py
if ($LASTEXITCODE -ne 0) { throw 'Icon validation failed.' }
$releaseVersion = & $BuildPython -c "from magic_converter import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0) { throw 'Could not read package version.' }
$releaseDirectory = Join-Path $PSScriptRoot "dist\v$releaseVersion"
$previousBuildPath = $env:PATH
$basePythonDirectory = & $BuildPython -c "import sys; print(sys.base_prefix)"
try {
    # Do not resolve native dependencies through unrelated Java/Office/tools.
    $env:PATH = @((Split-Path -Parent $BuildPython), $basePythonDirectory, (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    # Windows PowerShell treats normal PyInstaller stderr logs as errors when
    # redirected by a caller. The native exit code is the failure signal.
    $ErrorActionPreference = 'Continue'
    & $BuildPython -m PyInstaller --noconfirm --clean --distpath $releaseDirectory --workpath $WorkPath packaging\magic_converter.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    $ErrorActionPreference = 'Stop'
    $env:PATH = $previousBuildPath
}
Copy-Item -LiteralPath (Join-Path $WorkPath 'magic_converter\inventory\sbom.spdx.json') -Destination $releaseDirectory
Copy-Item -LiteralPath (Join-Path $WorkPath 'magic_converter\inventory\build-inputs.json') -Destination $releaseDirectory
& $BuildPython tools\validation\verify_release.py $releaseDirectory --report (Join-Path $releaseDirectory 'release-verification.json')
if ($LASTEXITCODE -ne 0) { throw 'Frozen release verification failed.' }
Write-Host "Built: $releaseDirectory\magic_converter.exe"
