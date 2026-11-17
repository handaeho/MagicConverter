# MagicConverter 3.0.5 for macOS

macOS 15 이상에서 실행됩니다. **이 Mac에 관하여**에서 칩이 Apple이면 arm64, 프로세서가 Intel이면 x86_64 ZIP을 내려받으세요.

[Apple Silicon 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.1/MagicConverter-3.0.5-macOS-arm64.zip) · [Intel 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.1/MagicConverter-3.0.5-macOS-x86_64.zip)

1. ZIP을 풀고 `MagicConverter.app`을 **응용 프로그램**으로 옮깁니다.
2. Finder에서 파일을 창에 끌어 넣거나 **파일 선택…**을 누릅니다.
3. 표시되는 변환 작업과 저장 위치를 선택하고 **변환 시작**을 누릅니다. **Finder에서 결과 보기**로 출력 파일을 찾을 수 있습니다.

앱에는 Rust 변환 엔진, PDFium, rhwp가 포함됩니다. 이미지·자막·압축·구조화 자료 변환, PDF → 편집 가능한 PPTX, DOCX → HWPX 등 선택한 파일에서 실행 가능한 작업이 목록에 나타납니다.

FFmpeg가 필요한 동영상·오디오 작업은 `brew install ffmpeg`로 설치한 뒤 실행할 수 있습니다. 앱은 일반적인 Apple Silicon 및 Intel Homebrew 경로를 확인합니다. Windows의 탐색기 휠, 트레이, Office COM 기반 인쇄 기능은 macOS 앱의 지원 범위에 포함되지 않습니다. 별도 Qt·MuPDF·HEIF/AVIF 엔진에 의존하는 일부 경로도 기본 패키지에서 사용할 수 없습니다.

ZIP에는 Apple Developer ID 서명과 공증이 없습니다. macOS의 기본 보안 정책에 따라 처음 실행할 때 차단될 수 있습니다.

CLI와 MCP 실행 파일은 앱에 들어 있습니다.

```sh
/Applications/MagicConverter.app/Contents/MacOS/magic_converter-cli --help
/Applications/MagicConverter.app/Contents/MacOS/magic_converter-cli convert --action pptx_editable -- "/path/to/input.pdf"
```

제거하려면 앱을 종료하고 `MagicConverter.app`을 휴지통으로 옮깁니다. 설정을 정리할 때는 `~/Library/Application Support/MagicConverter` 폴더를 확인하세요.

## 소스와 라이선스

- [MagicConverter 소스와 macOS 빌드 스크립트](https://github.com/handaeho/MagicConverter/tree/v3.0.5-macos.1)
- [PDFium 빌드와 upstream 소스 정보](https://github.com/bblanchon/pdfium-binaries/releases/tag/chromium%2F8076)
- [rhwp 0.8.7 대응 소스](https://github.com/edwardkim/rhwp/tree/v0.8.7)

라이선스와 고지 파일은 앱의 `Contents/Resources/Notices`에 포함됩니다.
