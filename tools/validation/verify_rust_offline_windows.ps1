param(
    [string] $InputDirectory = 'C:\MCInput',
    [string] $ResultDirectory = 'C:\MCResult',
    [switch] $VerifyExtended,
    [switch] $VerifyUpgrade
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$env:Path = "$env:SystemRoot\System32;$env:SystemRoot"
New-Item -ItemType Directory -Force -Path $ResultDirectory | Out-Null
$result = [ordered]@{
    status = 'running'
    windows_build = [System.Environment]::OSVersion.Version.ToString()
    timings_s = [ordered]@{}
}

function Measure-Program([string] $Phase, [string] $Program, [string[]] $Arguments) {
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $log = & $Program @Arguments 2>&1
    $code = $LASTEXITCODE
    $watch.Stop()
    $result.timings_s[$Phase] = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
    $log | Out-File -Encoding utf8 -LiteralPath (Join-Path $ResultDirectory "$Phase.log")
    if ($code -ne 0) { throw "$Phase exit $code" }
}

function Test-Mcp([string] $Program, [string] $Profile, [string] $Source, [string] $Output) {
    $settings = New-Object System.Diagnostics.ProcessStartInfo
    $settings.FileName = $Program
    $settings.Arguments = ('--data-dir "{0}"' -f $Profile)
    $settings.UseShellExecute = $false
    $settings.CreateNoWindow = $true
    $settings.RedirectStandardInput = $true
    $settings.RedirectStandardOutput = $true
    $settings.StandardOutputEncoding = [Text.Encoding]::UTF8
    $process = [Diagnostics.Process]::Start($settings)
    $encoding = [Text.UTF8Encoding]::new($false)
    try {
        function Send-Line([string] $Line) {
            $bytes = $encoding.GetBytes($Line + "`n")
            $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
            $process.StandardInput.BaseStream.Flush()
        }
        function Request($Message) {
            Send-Line ($Message | ConvertTo-Json -Compress -Depth 10)
            $read = $process.StandardOutput.ReadLineAsync()
            if (-not $read.Wait(10000) -or -not $read.Result) { throw 'MCP response timeout.' }
            return ($read.Result | ConvertFrom-Json)
        }
        $init = Request @{
            jsonrpc = '2.0'; id = 1; method = 'initialize'
            params = @{ protocolVersion = '2025-11-25'; capabilities = @{}; clientInfo = @{ name = 'Offline verification'; version = '1' } }
        }
        if ($init.result.protocolVersion -ne '2025-11-25') {
            throw ('MCP negotiation failed. ' + ($init | ConvertTo-Json -Compress -Depth 10))
        }
        Send-Line '{"jsonrpc":"2.0","method":"notifications/initialized"}'
        $list = Request @{ jsonrpc = '2.0'; id = 2; method = 'tools/list' }
        if ($list.result.tools[0].name -ne 'convert_files') { throw 'MCP tool discovery failed.' }
        $call = Request @{
            jsonrpc = '2.0'; id = 3; method = 'tools/call'
            params = @{name = 'convert_files'; arguments = @{paths = @($Source); action = 'tool:copy_original'; output = $Output}}
        }
        if (-not $call.result.structuredContent.ok -or $call.result.isError) { throw 'MCP copy failed.' }
        $copied = $call.result.structuredContent.jobs[0].outputs[0]
        if ((Get-FileHash -LiteralPath $copied).Hash -ne (Get-FileHash -LiteralPath $Source).Hash) {
            throw 'MCP copy changed the bytes.'
        }
    } finally {
        $process.StandardInput.BaseStream.Close()
        if (-not $process.WaitForExit(5000)) { $process.Kill() }
        $process.Dispose()
    }
}

function Install-PythonReference {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $extracted = Join-Path $env:LOCALAPPDATA 'MCReference'
    [IO.Compression.ZipFile]::ExtractToDirectory(
        (Join-Path $InputDirectory 'MagicConverter-2.2.39-Windows-x64.zip'), $extracted)
    $assembly = [Reflection.Assembly]::LoadFile((Join-Path $extracted 'MagicConverter-Setup.exe'))
    $root = Join-Path $env:LOCALAPPDATA 'Programs\MagicConverter'
    $target = Join-Path $root '2.2.39\magic-converter'
    $programs = [Environment]::GetFolderPath('Programs')
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    $flags = [Reflection.BindingFlags]'Static,NonPublic'
    $setup = $assembly.GetType('Setup', $true)
    $onProgress = [Action[string]]{ param([string] $Message) }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $null = $setup.GetMethod('InstallAt', $flags).Invoke(
        $null, @([string]::Copy($root), [string]::Copy($target), $onProgress))
    $null = $setup.GetMethod('CreateShortcut', $flags).Invoke(
        $null, @([string]::Copy($target), [string]::Copy($programs)))
    $result.timings_s['python-install'] = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
    $legacy = Join-Path $env:LOCALAPPDATA 'magic_converter\magic_converter'
    New-Item -ItemType Directory -Path $legacy -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $legacy 'settings.json'),
        '{"version":3,"interval_ms":41,"threshold":28,"presets":[{"id":"from-python","name":"Upgrade fixture","action_key":"image:png"}]}')
    [IO.File]::WriteAllText((Join-Path $legacy 'history.json'), '{"version":1,"entries":[]}')
    [IO.File]::WriteAllText((Join-Path $legacy 'automation.json'), '{"version":1,"rules":[]}')
    $shell = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\*\shell\MagicConverter\command')
    try {
        $shell.SetValue('', ('"{0}" --open "%1"' -f (Join-Path $target 'magic_converter.exe')))
    } finally {
        $shell.Dispose()
    }
    return @{
        target = $target
        profile = $legacy
        programs = $programs
        flags = $flags
        remover = [Reflection.Assembly]::LoadFile((Join-Path $extracted 'Uninstall.exe'))
    }
}

function Test-PythonUpgrade($Reference, [string] $Installed) {
    $current = Join-Path $env:LOCALAPPDATA 'MagicConverter\3.0\profile'
    foreach ($name in 'settings.json','history.json','automation.json') {
        if ((Get-FileHash -LiteralPath (Join-Path $current $name)).Hash -ne
            (Get-FileHash -LiteralPath (Join-Path $Reference.profile $name)).Hash) {
            throw "Profile migration changed $name."
        }
    }
    $shortcutPath = Join-Path $Reference.programs 'MagicConverter.lnk'
    $com = New-Object -ComObject WScript.Shell
    $gui = Join-Path $Installed 'magic_converter.exe'
    if ($com.CreateShortcut($shortcutPath).TargetPath -ne $gui) { throw '2.2.39 shortcut did not migrate.' }
    $shell = 'Registry::HKEY_CURRENT_USER\Software\Classes\*\shell\MagicConverter\command'
    if ((Get-Item -LiteralPath $shell).GetValue('') -ne ('"{0}" --open "%1"' -f $gui)) {
        throw '2.2.39 Explorer registration did not migrate.'
    }
    $native = [Microsoft.Win32.RegistryKey]::OpenBaseKey('CurrentUser','Registry64')
    $redirected = [Microsoft.Win32.RegistryKey]::OpenBaseKey('CurrentUser','Registry32')
    try {
        $arguments = [object[]]@([string]::Copy($env:LOCALAPPDATA), [string]::Copy($Reference.programs),
            [string]::Copy($Reference.target), $null)
        $arguments[3] = [Microsoft.Win32.RegistryKey[]]@($native,$redirected)
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $null = $Reference.remover.GetType('Uninstaller', $true).GetMethod('Remove', $Reference.flags).Invoke($null, $arguments)
        $result.timings_s['python-uninstall'] = [Math]::Round($watch.Elapsed.TotalSeconds, 3)
    } finally {
        $native.Dispose()
        $redirected.Dispose()
    }
    if ((Test-Path -LiteralPath (Join-Path $Reference.profile 'settings.json')) -or
        -not (Test-Path -LiteralPath (Join-Path $current 'settings.json')) -or
        $com.CreateShortcut($shortcutPath).TargetPath -ne $gui -or
        (Get-Item -LiteralPath $shell).GetValue('') -ne ('"{0}" --open "%1"' -f $gui)) {
        throw 'Removing 2.2.39 changed the 3.0 settings, shortcut, or Explorer registration.'
    }
    $result.upgrade = 'passed'
}

try {
    $setup = Join-Path $InputDirectory 'magic_converter-setup.exe'
    $uninstall = Join-Path $InputDirectory 'magic_converter-uninstall.exe'
    $parent = Join-Path $env:LOCALAPPDATA 'Programs\MagicConverter\3.0.0'
    $installArguments = @('install', '--quiet')
    if ($VerifyUpgrade) {
        $reference = Install-PythonReference
        $pythonWheelScript = Join-Path $InputDirectory 'verify_python_wheel.ps1'
        if (Test-Path -LiteralPath $pythonWheelScript) {
            & $pythonWheelScript -Installed $reference.target -InputDirectory $InputDirectory -ResultDirectory $ResultDirectory
            $pythonWheel = Get-Content -LiteralPath (Join-Path $ResultDirectory 'wheel.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($pythonWheel.status -ne 'passed') { throw "Python wheel validation failed: $($pythonWheel.error)" }
        }
        $parent = Join-Path $env:LOCALAPPDATA 'MC 3.0 custom install'
        $installArguments += @('--parent', $parent)
    }
    $installed = Join-Path $parent 'magic-converter'
    if (Test-Path -LiteralPath $installed) { throw 'A prior candidate is already installed.' }
    if (Get-Command python.exe,py.exe -ErrorAction SilentlyContinue) { throw 'Unexpected Python runtime in the test PATH.' }
    $result.python_in_path = $false
    $result.network_adapters_up = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | Select-Object -ExpandProperty Name)
    $result.installer_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash
    $result.installer_bytes = (Get-Item -LiteralPath $setup).Length
    $installReport = Join-Path $ResultDirectory 'install.json'
    Measure-Program 'install' $setup ($installArguments + @('--report', $installReport))
    if ($VerifyUpgrade) { Test-PythonUpgrade $reference $installed }
    $manifest = Get-Content -LiteralPath (Join-Path $installed '.magicconverter-install-id') -Raw | ConvertFrom-Json
    $result.installed_files = $manifest.payload.files.Count
    $result.installed_bytes = ($manifest.payload.files | Measure-Object -Property bytes -Sum).Sum
    foreach ($item in $manifest.payload.files) {
        $file = Join-Path $installed $item.path
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash -ne $item.sha256) {
            throw "Installed file hash mismatch for $($item.path)."
        }
    }
    $repeat = Join-Path $ResultDirectory 'repeat.json'
    Measure-Program 'repeat' $setup ($installArguments + @('--report', $repeat))
    if (-not (Get-Content -LiteralPath $repeat -Raw | ConvertFrom-Json).already_installed) { throw 'Install was not idempotent.' }

    $work = Join-Path $env:LOCALAPPDATA ('MCVerify-' + [Guid]::NewGuid().ToString('N'))
    $out = Join-Path $work 'output'
    $profile = Join-Path $work 'profile'
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $file = Join-Path $work '원본.txt'
    [System.IO.File]::WriteAllText($file, 'MagicConverter offline')
    $before = (Get-FileHash -LiteralPath $file).Hash
    $cli = Join-Path $installed 'magic_converter-cli.exe'
    $result.cli_sha256 = (Get-FileHash -LiteralPath $cli -Algorithm SHA256).Hash
    Measure-Program 'copy' $cli @('convert', '--data-dir', $profile, '--action', 'tool:copy_original', '--output', $out, $file)
    $copied = Join-Path $out '원본_copy.txt'
    if ((Get-FileHash -LiteralPath $copied).Hash -ne $before) { throw 'Copy changed the content.' }
    $wav = Join-Path $work 'tone.wav'
    $ffmpeg = Join-Path $installed 'vendor\ffmpeg\ffmpeg.exe'
    Measure-Program 'fixture' $ffmpeg @('-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', 'pcm_s24le', $wav)
    Measure-Program 'flac' $cli @('convert', '--data-dir', $profile, '--action', 'audio:flac', '--output', $out, $wav)
    $flac = Join-Path $out 'tone_converted.flac'
    if (-not (Test-Path -LiteralPath $flac -PathType Leaf)) { throw 'Missing FLAC output.' }
    $result.copy_sha256 = $before
    $result.flac_sha256 = (Get-FileHash -LiteralPath $flac).Hash
    if ($VerifyExtended) {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        if (-not ('MagicDropFixture' -as [type])) {
            Add-Type -Path (Join-Path $InputDirectory 'MagicDropFixture.cs') -ReferencedAssemblies System.Windows.Forms, System.Drawing
        }
        $png = Join-Path $work 'fixture.png'
        $bitmap = New-Object Drawing.Bitmap 480,360
        try {
            $bitmap.SetPixel(2, 2, [Drawing.Color]::Blue)
            $bitmap.Save($png, [Drawing.Imaging.ImageFormat]::Png)
        } finally {
            $bitmap.Dispose()
        }
        $pdf = Join-Path $out 'fixture_converted.pdf'
        Measure-Program 'image-pdf' $cli @('convert', '--data-dir', $profile, '--action', 'document:pdf', '--output', $out, $png)
        Measure-Program 'pdf-png' $cli @('convert', '--data-dir', $profile, '--action', 'document:png', '--output', $out, $pdf)
        Measure-Program 'image-heic' $cli @('convert', '--data-dir', $profile, '--action', 'image:heic', '--output', $out, $png)
        $heic = Join-Path $out 'fixture_converted.heic'
        Measure-Program 'heic-avif' $cli @('convert', '--data-dir', $profile, '--action', 'image:avif', '--output', $out, $heic)
        Measure-Program 'avif-png' $cli @('convert', '--data-dir', $profile, '--action', 'image:png', '--output', $out, (Join-Path $out 'fixture_converted_converted.avif'))
        $result.bundled_pdfium_and_heif = $true
        if (Test-Path -LiteralPath (Join-Path $installed 'vendor\mupdf\magic_mupdf_bridge.dll')) {
            Measure-Program 'pdf-cbz' $cli @('convert', '--data-dir', $profile, '--action', 'ebook:cbz', '--output', $out, $pdf)
            Measure-Program 'pdf-slides' $cli @('convert', '--data-dir', $profile, '--action', 'pptx_image', '--output', $out, $pdf)
            $result.bundled_mupdf = $true
        }
        if (Test-Path -LiteralPath (Join-Path $installed 'vendor\rhwp\rhwp.exe')) {
            $hwpx = Join-Path $InputDirectory 'fixture.hwpx'
            Measure-Program 'hwpx-hwp' $cli @('convert', '--data-dir', $profile, '--action', 'document:hwp', '--output', $out, $hwpx)
            $hwp = Join-Path $out 'fixture_converted.hwp'
            Measure-Program 'hwp-hwpx' $cli @('convert', '--data-dir', $profile, '--action', 'document:hwpx', '--output', $out, $hwp)
            $result.bundled_rhwp = $true
        }
        $textPdf = Join-Path $out '원본_converted.pdf'
        Measure-Program 'qt-text-pdf' $cli @('convert', '--data-dir', $profile, '--action', 'document:pdf', '--output', $out, $file)
        Measure-Program 'text-epub' $cli @('convert', '--data-dir', $profile, '--action', 'document:epub', '--output', $out, $file)
        Measure-Program 'pdf-docx' $cli @('convert', '--data-dir', $profile, '--action', 'document:docx', '--output', $out, $textPdf)
        Measure-Program 'pdf-xlsx' $cli @('convert', '--data-dir', $profile, '--action', 'document:xlsx', '--output', $out, $textPdf)
        Measure-Program 'pdf-pptx-editable' $cli @('convert', '--data-dir', $profile, '--action', 'pptx_editable', '--output', $out, $textPdf)
        $docx = Join-Path $out '원본_converted_converted.docx'
        Measure-Program 'docx-hwpx' $cli @('convert', '--data-dir', $profile, '--action', 'document:hwpx', '--output', $out, $docx)
        $hwpx = Join-Path $out '원본_converted_converted_converted.hwpx'
        Measure-Program 'hwpx-docx' $cli @('convert', '--data-dir', $profile, '--action', 'document:docx', '--output', $out, $hwpx)
        Measure-Program 'docx-pdf-offline' $cli @('convert', '--data-dir', $profile, '--action', 'document:pdf', '--output', $out, $docx)
        $xlsx = Join-Path $out '원본_converted_converted.xlsx'
        Measure-Program 'xlsx-pdf-offline' $cli @('convert', '--data-dir', $profile, '--action', 'document:pdf', '--output', $out, $xlsx)
        $pptx = Join-Path $out '원본_converted_converted.pptx'
        Measure-Program 'pptx-pdf-offline' $cli @('convert', '--data-dir', $profile, '--action', 'document:pdf', '--output', $out, $pptx)
        $result.bundled_qt_and_document_models = $true
        $mcp = Join-Path $installed 'magic_converter-mcp.exe'
        Test-Mcp $mcp $profile $file (Join-Path $work 'mcp-output')
        $result.mcp_sha256 = (Get-FileHash -LiteralPath $mcp -Algorithm SHA256).Hash
    }

    $gui = Join-Path $installed 'magic_converter.exe'
    $shortcut = Join-Path ([System.Environment]::GetFolderPath('Programs')) 'MagicConverter.lnk'
    $result.gui_in_bundle = Test-Path -LiteralPath $gui
    if ($result.gui_in_bundle -and -not (Test-Path -LiteralPath $shortcut -PathType Leaf)) { throw 'Missing Start Menu shortcut.' }
    if ($result.gui_in_bundle) {
        if ($VerifyExtended) {
            $dropOut = Join-Path $work 'drop-output'
            $recipe = @{
                version = 3; enabled = $true; interval_ms = 30; threshold = 15; release_modifier_inside = $true
                convert_profile = 'test-convert'; action_profile = 'test-action'
                profiles = @(
                    @{id = 'test-convert'; name = 'Images'; mode = 'convert'; preset_ids = @('test-png')},
                    @{id = 'test-action'; name = 'Actions'; mode = 'action'; preset_ids = @('test-copy', 'test-path', 'test-terminal')}
                )
                presets = @(
                    @{id = 'test-png'; name = 'PNG'; action_key = 'image:png'},
                    @{id = 'test-copy'; name = 'Copy'; action_key = 'tool:copy_original'}
                    @{id = 'test-path'; name = 'Copy path'; action_key = 'copy_path'}
                    @{id = 'test-terminal'; name = 'Open terminal'; action_key = 'terminal'}
                )
                output = @{location = 'folder'; directory = $dropOut; template = '{stem}_{tag}'; collision = 'error'}
            }
            New-Item -ItemType Directory -Force -Path $profile | Out-Null
            [System.IO.File]::WriteAllText((Join-Path $profile 'settings.json'), ($recipe | ConvertTo-Json -Depth 10))
        }
        $ready = [Diagnostics.Stopwatch]::StartNew()
        $guiProcess = Start-Process -FilePath $gui -ArgumentList @('--tray', '--data-dir', ('"{0}"' -f $profile)) -PassThru
        try {
            $statusReport = Join-Path $ResultDirectory 'gui-status.json'
            Measure-Program 'gui-start' $cli @('status', '--data-dir', $profile, '--report', $statusReport)
            $result.gui_ready_ms = $ready.Elapsed.TotalMilliseconds
            $instance = (Get-Content -LiteralPath $statusReport -Raw | ConvertFrom-Json).instance
            if (-not $instance) { throw 'Installed tray IPC did not start.' }
            $shellReport = Join-Path $ResultDirectory 'shell.json'
            Measure-Program 'shell-install' $cli @('shell', 'install', '--data-dir', $profile, '--report', $shellReport)
            if (-not (Get-Content -LiteralPath $shellReport -Raw | ConvertFrom-Json).command.Contains($gui)) {
                throw 'Explorer menu did not point to the installed GUI.'
            }
            $guiOut = Join-Path $work 'gui-output'
            Measure-Program 'gui-send' $cli @('send', '--data-dir', $profile, '--action', 'tool:copy_original',
                '--request-id', 'offline-gui-test', '--output', $guiOut, $file)
            $guiCopy = Join-Path $guiOut '원본_copy.txt'
            $deadline = [DateTime]::UtcNow.AddSeconds(10)
            while (-not (Test-Path -LiteralPath $guiCopy) -and [DateTime]::UtcNow -lt $deadline) {
                Start-Sleep -Milliseconds 100
            }
            if ((Get-FileHash -LiteralPath $guiCopy).Hash -ne $before) { throw 'Tray conversion changed the source content.' }
            if ($VerifyExtended) {
                $result.drops = [ordered]@{}
                $result.drop_hashes = [ordered]@{}
                $result.drop_performance = [ordered]@{}
                foreach ($case in @(
                    @{label = 'held-shift-rejected'; path = $png; uri = ''; key = 16; release = $false; effect = 'None'; file = 'fixture_converted.png'},
                    @{label = 'shift-file'; path = $png; uri = ''; key = 16; release = $true; effect = 'Copy'; file = 'fixture_converted.png'},
                    @{label = 'ctrl-file'; path = $file; uri = ''; key = 17; release = $true; effect = 'Copy'; file = '원본_copy.txt'},
                    @{label = 'shift-web'; path = ''; uri = ('data:image/png;base64,' + [Convert]::ToBase64String([IO.File]::ReadAllBytes($png))); key = 16; release = $true; effect = 'Copy'; file = 'image_converted.png'}
                )) {
                    $uri = if ($case.uri) { [string] $case.uri } else { $null }
                    $screenshot = Join-Path $ResultDirectory "$($case.label).png"
                    $effect = [MagicDropFixture]::Run($case.path, $uri, $case.key, $case.release, $false, $screenshot)
                    $result.drops[$case.label] = $effect
                    $result.drop_performance[$case.label] = @{wheel_ms = [MagicDropFixture]::WheelMilliseconds}
                    if ($effect -ne $case.effect) { throw "$($case.label) gave OLE effect $effect." }
                    if ($case.effect -eq 'Copy') {
                        $deadline = [DateTime]::UtcNow.AddSeconds(10)
                        $target = Join-Path $dropOut $case.file
                        while (-not (Test-Path -LiteralPath $target) -and [DateTime]::UtcNow -lt $deadline) {
                            Start-Sleep -Milliseconds 10
                        }
                        if (-not (Test-Path -LiteralPath $target)) { throw "$($case.label) did not publish $($case.file)." }
                        $result.drop_performance[$case.label].release_to_output_ms = 1000.0 *
                            ([Diagnostics.Stopwatch]::GetTimestamp() - [MagicDropFixture]::ReleaseTimestamp) / [Diagnostics.Stopwatch]::Frequency
                        $result.drop_hashes[$case.label] = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
                    } elseif (Test-Path -LiteralPath (Join-Path $dropOut $case.file)) {
                        throw 'A held modifier published an output before release.'
                    }
                }
                if ((Get-FileHash -LiteralPath (Join-Path $dropOut '원본_copy.txt')).Hash -ne $before) { throw 'Ctrl drop copy changed the content.' }
                $effect = [MagicDropFixture]::Run($file, $null, 17, $true, $false, (Join-Path $ResultDirectory 'ctrl-path.png'), 1, 3)
                if ($effect -ne 'Copy' -or [Windows.Forms.Clipboard]::GetText() -cne $file) {
                    throw 'Ctrl path copy did not publish the original path.'
                }
                $result.drops['ctrl-path'] = $effect
                $cliPid = (Get-CimInstance Win32_Process -Filter "ParentProcessId=$($guiProcess.Id)" |
                    Where-Object Name -eq 'magic_converter-cli.exe' | Select-Object -First 1 -ExpandProperty ProcessId)
                if (-not $cliPid) { throw 'Could not identify the installed desktop process.' }
                $effect = [MagicDropFixture]::Run($file, $null, 17, $true, $false, (Join-Path $ResultDirectory 'ctrl-terminal.png'), 2, 3)
                $terminal = Get-CimInstance Win32_Process -Filter "ParentProcessId=$cliPid AND Name='powershell.exe'"
                try {
                    if ($effect -ne 'Copy' -or @($terminal).Count -ne 1 -or
                        $terminal.CommandLine -notmatch '-NoLogo -NoProfile -NoExit$') {
                        throw 'Ctrl terminal did not start one PowerShell session with the fixed argument list.'
                    }
                    $result.drops['ctrl-terminal'] = $effect
                } finally {
                    foreach ($process in $terminal) { Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue }
                }
            }
            Measure-Program 'gui-stop' $cli @('stop', '--data-dir', $profile, '--instance', $instance)
            if (-not $guiProcess.WaitForExit(10000)) { throw 'Installed GUI did not stop.' }
            $result.gui_conversion = $true
        } finally {
            if (-not $guiProcess.HasExited) { Stop-Process -Id $guiProcess.Id }
        }
    }
    $sentinel = Join-Path $installed 'user-preserved.txt'
    [System.IO.File]::WriteAllText($sentinel, 'leave me')
    $uninstallReport = Join-Path $ResultDirectory 'uninstall.json'
    Measure-Program 'uninstall' $uninstall @('uninstall', '--quiet', '--report', $uninstallReport)
    $remaining = @(Get-ChildItem -LiteralPath $installed -Force | Select-Object -ExpandProperty Name)
    if ($remaining.Count -ne 1 -or $remaining[0] -ne 'user-preserved.txt') { throw "Unexpected remaining files $remaining." }
    if ((Get-Content -LiteralPath $sentinel -Raw) -ne 'leave me' -or
        (Get-FileHash -LiteralPath $copied).Hash -ne $before -or
        (Get-FileHash -LiteralPath $file).Hash -ne $before -or
        (Test-Path -LiteralPath $shortcut)) { throw 'Uninstall changed user data or left the application shortcut.' }
    $key = 'Registry::HKEY_CURRENT_USER\Software\Classes\*\shell\MagicConverter'
    if ($result.gui_in_bundle -and (Test-Path -LiteralPath $key)) { throw 'Uninstall left the Explorer menu.' }
    $result.preserved = $remaining
    $result.status = 'passed'
} catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
    $result.error_location = $_.ScriptStackTrace
} finally {
    $result | ConvertTo-Json -Depth 8 | Out-File -Encoding utf8 -LiteralPath (Join-Path $ResultDirectory 'report.json')
}

if ($result.status -ne 'passed') { exit 1 }
