# MagicConverter 3.0.5 for macOS

macOS 15 이상용입니다. **이 Mac에 관하여**에서 칩이 Apple M 계열이면 Apple Silicon, 프로세서가 Intel이면 Intel 배포 파일을 선택합니다. 이번 수정 배포의 태그는 `v3.0.5-macos.2`, 앱 빌드 번호는 `30502`입니다.

| Mac | 설치 마법사 | 직접 설치 ZIP |
| --- | --- | --- |
| Apple Silicon | [PKG 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.2/MagicConverter-3.0.5-macOS-arm64.pkg) | [ZIP 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.2/MagicConverter-3.0.5-macOS-arm64.zip) |
| Intel | [PKG 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.2/MagicConverter-3.0.5-macOS-x86_64.pkg) | [ZIP 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.2/MagicConverter-3.0.5-macOS-x86_64.zip) |

## 설치와 첫 실행

1. PKG를 실행해 안내와 라이선스를 확인하고 사용자 홈의 **Applications** 폴더에 설치합니다. ZIP을 사용하면 `MagicConverter.app`을 **응용 프로그램**으로 옮깁니다. 기존 앱이 실행 중이면 먼저 종료합니다.
2. 응용 프로그램에서 MagicConverter를 실행합니다. 앱과 메뉴 막대에 MagicConverter 아이콘이 표시됩니다.
3. 파일을 드래그해 휠에서 작업을 고르거나 앱 창의 **변환할 파일 고르기…**로 파일과 작업을 선택합니다.

PKG는 macOS 기본 설치 마법사로 `~/Applications/MagicConverter.app`에 설치합니다. 관리자 권한 없이 설치·실행·제거할 수 있습니다. Spotlight에서 MagicConverter를 검색해 실행할 수 있습니다. 인터넷 없이 설치·로컬 변환할 수 있고 Python·Homebrew·별도 문서 프로그램 설치가 필요하지 않습니다. 구형 PPT와 인쇄 설정 PDF까지 지원하는 문서 엔진을 포함하므로 이전 기본 Mac 패키지보다 다운로드·설치 용량이 커집니다.

유료 Apple Developer ID 서명·공증은 없습니다. 첫 실행을 macOS가 차단하면 해당 앱을 한 번 열어 본 뒤 **시스템 설정 → 개인정보 보호 및 보안 → 그래도 열기(확인 없이 열기)**에서 이 앱의 실행을 허용합니다. 회사 관리 정책이 허용을 제한할 수 있습니다. [Apple의 앱 실행 안내](https://support.apple.com/ko-kr/102445)를 참고하세요.

## Windows와 같은 작업 방식

- **Shift 변환 휠:** Finder에서 파일을 먼저 드래그하고 Shift를 누른 뒤 원하는 항목에 놓기.
- **Control 빠른 작업 휠:** 파일을 먼저 드래그하고 Control을 누른 뒤 도구 항목에 놓기. Control+클릭은 macOS의 보조 클릭이므로 드래그 시작 후 Control 누르기.
- **브라우저 입력:** 웹 이미지의 드래그, 파일 URL·웹 URL, 이미지 데이터와 브라우저가 전달하는 파일 받기. 임시 입력의 기본 결과 위치는 `~/Downloads/MagicConverter`이며 프리셋 출력 폴더 지정 가능.
- **설정:** 클릭으로 고급 옵션과 프리셋·휠 항목 편집, 설정 가져오기·내보내기·백업 복구, 작업 관리·폴더 감시와 결과 기록.
- **진행 상태:** 화면 오른쪽 아래에 처리 파일명과 완료 개수 표시. 여러 변환 요청의 순차 처리와 전체 취소 지원.
- **빠른 작업:** 메타데이터 제거, 파일 도구, 경로 복사와 Terminal 열기. 여러 파일 입력에는 공통 변환 작업 표시.
- **상주 실행:** 창을 닫아도 메뉴 막대에서 계속 실행. 아이콘 또는 Dock에서 설정 창을 다시 열고 메뉴의 **MagicConverter 종료**로 종료.

macOS의 Command·Option 조합은 휠을 열지 않습니다. macOS 표준 파일 대화상자·메뉴 막대·설치 마법사를 사용하므로 Windows와 창 모양은 다릅니다.

## 변환과 지원 조건

앱 안에 Rust 엔진, FFmpeg·FFprobe, Qt·MuPDF·PDFium, rhwp와 HEIC/AVIF 엔진을 포함합니다. 문서·PDF·이미지·미디어·압축·자막·자료·글꼴 변환, PDF → 편집 가능한 PPTX, DOCX → HWPX 및 도구 작업은 Windows 3.0.5와 같은 변환 경로를 사용합니다. 기본 대체 글꼴도 포함합니다.

원본에 사용한 글꼴이 Mac에 없으면 대체 글꼴로 출력하므로 줄바꿈과 조판이 달라질 수 있습니다. 복잡한 Office 문서의 원본 프로그램 출력과 기본 문서 모델의 출력도 차이가 날 수 있습니다. 구형 `.ppt`의 PDF·이미지 출력과 **Excel 인쇄 PDF**는 앱 내부의 LibreOffice 인쇄 엔진으로 처리합니다. 별도 Office 설치가 필요하지 않습니다. Windows의 Microsoft Office와 Mac의 LibreOffice는 원본 해석이 달라 일부 조판 차이가 발생할 수 있습니다. 일반 **PDF** 출력은 공통 문서 엔진을 사용합니다.

CSV·TSV 변환에는 표 형태의 자료가, JSON Lines 출력에는 최상위 배열이 필요합니다. 지원 경로에 등록돼 있어도 원본이 해당 출력의 내용 조건을 충족해야 합니다. [전체 지원 형식과 조건](../../README.md#지원-확장자와-변환-가능-목록)을 참고하세요.

## CLI와 자동화

```sh
"$HOME/Applications/MagicConverter.app/Contents/MacOS/magic_converter-cli" --help
"$HOME/Applications/MagicConverter.app/Contents/MacOS/magic_converter-cli" convert --action pptx_editable -- "/path/to/input.pdf"
```

MCP 클라이언트에 `Contents/MacOS/magic_converter-mcp`를 stdio 서버로 등록합니다. `watch`, `serve`, `send`, `status`, `cancel`, `stop` 명령도 제공합니다. 같은 사용자 계정의 로컬 IPC는 Unix 소켓을 사용합니다. 설정과 결과 기록은 `~/Library/Application Support/MagicConverter/3.0/profile`에 보관됩니다.

## 제거

앱 메뉴의 **MagicConverter 제거… → 제거**를 선택합니다. 실행 중인 변환이 있으면 사유를 안내하고 제거를 중단합니다. 제거 시 앱을 휴지통으로 옮기며 설정·기록·원본·결과 파일은 보존합니다. 앱을 정상 종료한 뒤 Finder에서 휴지통으로 옮겨도 됩니다.

## 소스와 라이선스

- [MagicConverter 대응 소스와 macOS 빌드 스크립트](https://github.com/handaeho/MagicConverter/tree/v3.0.5-macos.2)
- [PDFium 빌드와 소스 정보](https://github.com/bblanchon/pdfium-binaries/releases/tag/chromium%2F8076)
- [rhwp 0.8.7 대응 소스](https://github.com/edwardkim/rhwp/tree/v0.8.7)
- [LibreOffice 대응 소스](https://download.documentfoundation.org/libreoffice/src/26.8.3/) · [라이선스](https://www.libreoffice.org/licenses/)

라이선스·고지, 포함 글꼴의 라이선스, 네이티브 라이브러리 목록과 빌드 입력 정보는 앱의 `Contents/Resources/Notices`에 있습니다. 배포 파일과 함께 제공되는 SHA-256 파일로 다운로드 무결성을 확인할 수 있습니다.
