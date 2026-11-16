# MagicConverter Windows 빌드

## Rust 3.0.0 Windows x64

Windows x64에서 Rust MSVC 도구 체인, Visual Studio C++ 빌드 도구, Windows SDK와 Python 3.11 이상을 준비합니다. Python은 빌드 준비에 사용합니다. 명령은 x64 개발자 PowerShell의 저장소 루트에서 실행합니다.

1. 애플리케이션 및 설치기의 `Cargo.lock`에 지정된 레지스트리 패키지를 Cargo 캐시에 준비합니다. 3.0.0 릴리스의 `MagicConverter-3.0.0-Cargo-source.zip`에 패키지 원문과 해시가 있습니다.
2. `python tools/build/prepare_rust_heif.py --help`와 `python tools/build/prepare_rust_qt.py --help`를 확인하고 잠금 파일과 일치하는 HEIF·AVIF 및 Qt 개발 입력을 준비합니다.
3. PDFium DLL, PyMuPDF 개발 파일, FFmpeg 공유 빌드와 rhwp를 준비합니다. `tools/build/rust_*_runtime.json`과 [Windows 빌드 스크립트](../../tools/build/build_rust_windows_offline.ps1)에 헤더와 DLL의 요구 버전 및 해시가 있습니다. rhwp와 런타임 파일은 `vendor/rhwp`에 둡니다. [rhwp 빌드 문서](../../vendor/rhwp/BUILD.md)
4. `.venv`를 만들고 아래 명령을 실행합니다. `-PublishedRuntimeAssets`에 지정한 디렉터리에 대응하는 2.2.39 공개 아카이브 세 개를 둡니다. 스크립트는 공개 릴리스에 사용된 엔진 파일을 아카이브에서 검증해 배치합니다. [아카이브, 라이선스와 DLL 교체 절차](../release/RUST_WINDOWS_INSTALLER_SOURCE.md)

```powershell
python -m venv .venv
.\build.ps1 -HeifDirectory C:\cache\heif -QtRuntimeDirectory C:\cache\qt `
  -FfmpegRuntimeDirectory C:\cache\ffmpeg -PdfiumLibrary C:\cache\pdfium.dll `
  -MuPdfDirectory C:\cache\mupdf -PublishedRuntimeAssets C:\cache\published-2.2.39
```

기본 출력은 `dist/v3.0.0`입니다. 재실행 시 비어 있는 새 `-OutputDirectory`와 `-PayloadDirectory`를 지정합니다. 빌드가 끝나면 설치 EXE, 제거 EXE, Windows ZIP과 SHA-256 목록이 생성됩니다.

## Python 2.2.39 비교 기준판

이전 배포판과 대응 소스의 빌드 방법은 2.2.39 태그에 있습니다. [Python 2.2.39 Windows 빌드](https://github.com/handaeho/MagicConverter/blob/v2.2.39/docs/release/WINDOWS_INSTALLER_SOURCE.md)
