# Run from a clean Windows checkout; works with GitBucket/Jenkins or GitHub runners.
param([ValidateSet('All','Build','Source','Frozen','External','Reproduce')][string]$Phase='All', [switch]$Offline)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '../..'))
if ($Phase -eq 'All') {
    foreach ($part in @('Build','Source','Frozen','External','Reproduce')) {
        & $PSCommandPath -Phase $part -Offline:$Offline
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }
    exit 0
}
# Windows PowerShell 5 treats redirected native stderr as ErrorRecords even
# on exit 0. A native command's exit code, not informational stderr, is its gate.
function Invoke-CiNative([scriptblock]$Command) {
    $ErrorActionPreference='Continue'
    & $Command
    $nativeExitCode=$LASTEXITCODE
    if ($nativeExitCode -ne 0) { throw "CI native command failed: $nativeExitCode" }
}
New-Item -ItemType Directory -Force .test-artifacts | Out-Null
$receipt=Join-Path (Get-Location) ".test-artifacts/ci-phase-$Phase.json"
$record=@{run_id=[guid]::NewGuid().ToString('N'); phase=$Phase; status='running'; version=$null; started_utc=[DateTime]::UtcNow.ToString('o'); remote_execution='not_attested'}
$record | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
$code=1
try {
    $python = if (Test-Path './.venv/Scripts/python.exe') { './.venv/Scripts/python.exe' } else { 'py' }
    $version = & $python -c "from magic_converter import __version__; print(__version__)"
    if ($LASTEXITCODE) { throw 'Cannot read package version' }
    $record.version=$version
    $env:MAGIC_CONVERTER_RELEASE_DIR="dist/v$version"
    $env:PYTHONUTF8='1'
    $env:MAGIC_CONVERTER_TEST_OFFICE='0'
    $env:MAGIC_CONVERTER_PROFILE_DIR=Join-Path (Get-Location) '.test-artifacts/ci-profile'
    if ($Phase -ne 'Build' -and -not (Test-Path './.venv/Scripts/python.exe')) { throw 'Run the locked Build phase first' }
    switch ($Phase) {
        'Build' {
            ./build.ps1 -Offline:$Offline
            if ($LASTEXITCODE) { throw "Locked build failed: $LASTEXITCODE" }
        }
        'Source' {
            New-Item -ItemType Directory -Force .test-artifacts/ci | Out-Null
            Invoke-CiNative { ./.venv/Scripts/python.exe -m pip check }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe -m unittest discover -s tests -v *> .test-artifacts/ci/unit.log }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_license_checkout.py --all-inputs --report .test-artifacts/ci/original-checkouts.json }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_source_export.py --report .test-artifacts/ci/original-export.json }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_document_failure_gate.py --report .test-artifacts/ci/injected-document-failure.json }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_build_failure_gates.py $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe --report .test-artifacts/ci/injected-build-failure.json }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe run_magic_converter.py --self-test .test-artifacts/ci/source.json }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_colors.py --output .test-artifacts/ci/pdf-colours-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_text_direction.py --output .test-artifacts/ci/pdf-directions-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_effects.py --output .test-artifacts/ci/pdf-effects-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_clips.py --output .test-artifacts/ci/pdf-clips-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_opaque_overlap.py --output .test-artifacts/ci/pdf-opaque-overlap-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_glyphs.py --output .test-artifacts/ci/pdf-glyphs-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_glyph_alpha.py --output .test-artifacts/ci/pdf-glyph-alpha-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_complex_layout.py --output .test-artifacts/ci/pdf-complex-layout-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_direct_api_rss.py --output .test-artifacts/ci/direct-api-rss }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_registered_api_rss.py --output .test-artifacts/ci/registered-api-rss }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_raw_api_rss.py --output .test-artifacts/ci/raw-api-rss }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_rich_api_rss.py --output .test-artifacts/ci/rich-api-rss }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_shell_virtual_files.py --output .test-artifacts/ci/shell-virtual-files }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }

            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_type3_curves.py --output .test-artifacts/ci/type3-curves-source }
            if ($LASTEXITCODE) { throw 'Type3 curve gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_type3_topology.py --output .test-artifacts/ci/type3-topology-source }
            if ($LASTEXITCODE) { throw 'Type3 topology gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_type3_compound_curves.py --output .test-artifacts/ci/type3-compound-source }
            if ($LASTEXITCODE) { throw 'Compound Type3 curve gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_metrics.py --output .test-artifacts/ci/hwpx-metrics-source }
            if ($LASTEXITCODE) { throw 'HWPX character metrics gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_paragraph_layout.py --output .test-artifacts/ci/paragraph-layout-source }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_character_spacing.py --output .test-artifacts/ci/character-spacing-source }
            if ($LASTEXITCODE) { throw 'Paragraph layout gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_origin_contract.py --output .test-artifacts/ci/origin-contract-source }
            if ($LASTEXITCODE) { throw 'HWPX origin/font contract gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_roman_fonts.py --output .test-artifacts/ci/roman-fonts-source }
            if ($LASTEXITCODE) { throw 'HWPX Roman font gate failed' }

            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_table_rules.py --output .test-artifacts/ci/table-rules-source }
            if ($LASTEXITCODE) { throw "Table rule gate failed" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_script_fonts.py --output .test-artifacts/ci/script-fonts-source }
            if ($LASTEXITCODE) { throw "HWPX script font gate failed" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_single_line_cells.py --output .test-artifacts/ci/single-line-cells-source }
            if ($LASTEXITCODE) { throw "Single-line cell gate failed" }

            Invoke-CiNative { ./.venv/Scripts/python.exe tools/build/prepare_source_review.py --output .test-artifacts/ci/source-review }
            if ($LASTEXITCODE) { throw "Source review preparation failed" }
        }
        'Frozen' {
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_raw_api_rss.py --output .test-artifacts/ci/raw-api-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_rich_api_rss.py --output .test-artifacts/ci/rich-api-cli --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_rich_api_rss.py --output .test-artifacts/ci/rich-api-gui --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_exe.py *> .test-artifacts/ci/gui.log }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_cli.py $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe --report .test-artifacts/ci/cli.json }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_library_workers.py --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe --output .test-artifacts/ci/library-workers }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_watch_once.py --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe --output .test-artifacts/ci/watch-once }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_office_workers.py --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe --gui-executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter.exe --output .test-artifacts/ci/office-workers }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_colors.py --output .test-artifacts/ci/pdf-colours-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_text_direction.py --output .test-artifacts/ci/pdf-directions-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_effects.py --output .test-artifacts/ci/pdf-effects-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_clips.py --output .test-artifacts/ci/pdf-clips-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_opaque_overlap.py --output .test-artifacts/ci/pdf-opaque-overlap-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_glyphs.py --output .test-artifacts/ci/pdf-glyphs-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_glyph_alpha.py --output .test-artifacts/ci/pdf-glyph-alpha-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_complex_layout.py --output .test-artifacts/ci/pdf-complex-layout-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_large_input_rss.py --output .test-artifacts/ci/large-input-rss --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }

            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_type3_curves.py --output .test-artifacts/ci/type3-curves-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'Type3 curve gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_type3_topology.py --output .test-artifacts/ci/type3-topology-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'Type3 topology gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_type3_compound_curves.py --output .test-artifacts/ci/type3-compound-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'Compound Type3 curve gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_metrics.py --output .test-artifacts/ci/hwpx-metrics-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'HWPX character metrics gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_paragraph_layout.py --output .test-artifacts/ci/paragraph-layout-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_character_spacing.py --output .test-artifacts/ci/character-spacing-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'Paragraph layout gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_origin_contract.py --output .test-artifacts/ci/origin-contract-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'HWPX origin/font contract gate failed' }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_roman_fonts.py --output .test-artifacts/ci/roman-fonts-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw 'HWPX Roman font gate failed' }

            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_table_rules.py --output .test-artifacts/ci/table-rules-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "Table rule gate failed" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_hwpx_script_fonts.py --output .test-artifacts/ci/script-fonts-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "HWPX script font gate failed" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_pdf_single_line_cells.py --output .test-artifacts/ci/single-line-cells-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "Single-line cell gate failed" }
        }
        'External' {
            java -version
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/dependencies/fetch_epubcheck.py }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/probe_epub_integrity.py --strict --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/epub-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/probe_epub_integrity.py --strict --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/epub-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_epub_followups.py --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/followup-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_epub_followups.py --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/followup-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_resource_limits.py --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/resources --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_browser_book_stage.py --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/browser-book-source }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_browser_book_stage.py --jar .build-cache/validators/epubcheck-5.4.0/epubcheck.jar --output .test-artifacts/ci/browser-book-frozen --executable $env:MAGIC_CONVERTER_RELEASE_DIR/magic_converter-cli.exe }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
        }
        'Reproduce' {
            Invoke-CiNative { ./.venv/Scripts/python.exe tools/validation/verify_repro_build.py --output .test-artifacts/ci-reproduction }
            if ($LASTEXITCODE) { throw "CI command failed: $LASTEXITCODE" }
        }
    }
    $record.status='passed'; $code=0
} catch {
    $record.status='failed'; $record.error=$_.Exception.Message
    Write-Host $_.Exception.Message
} finally {
    $record.finished_utc=[DateTime]::UtcNow.ToString('o')
    $record | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding UTF8
}
exit $code
