# MagicConverter 3.0.5 — Windows·macOS 파일 변환기

탐색기나 Finder에서 파일을 끌어 문서, PDF, 이미지, 표와 미디어를 변환하는 Windows·macOS 앱입니다.

[Windows 설치](#windows에서-시작하기) · [macOS 설치](#macos에서-시작하기) · [CLI로 시작하기](#cli로-시작하기)

Windows 10·11 x64, macOS 15 이상에서 동작하는 Rust 앱입니다. <br>
필수 변환 엔진을 포함하므로 인터넷 연결 없이 설치하고 로컬 파일을 변환할 수 있습니다.

앱에서 **변환할 파일 고르기…**로 원본을 선택하고 작업을 고른 뒤 **변환 시작**을 누릅니다. <br>
파일을 드래그할 때 **Shift**를 누르면 변환 휠이, **Ctrl(Control)**을 누르면 빠른 작업 휠이 열립니다. <br>
화면 오른쪽 아래에서 처리 중인 파일명과 완료 개수를 확인할 수 있습니다.

고급 설정에서는 목록과 선택 창에서 값을 고를 수 있습니다. <br>
설정 항목에 마우스를 올리면 한국어 도움말이 표시됩니다.

## Windows에서 시작하기

[MagicConverter 3.0.5 Windows x64 설치 ZIP 다운로드](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5/MagicConverter-3.0.5-Windows-x64.zip)

Windows 10·11 x64를 지원합니다. 다운로드 및 압축 해제 후 **설치 마법사의 안내**에 따르세요.

1. ZIP을 풀고 `magic_converter-setup.exe`를 실행합니다. **다음**을 누르고 라이선스 조건에 동의합니다.
2. 기본 설치 위치를 사용하거나 **찾아보기**에서 상위 폴더를 고릅니다. 실제 설치 위치를 확인한 뒤 **설치**를 누릅니다.
3. **MagicConverter 실행**을 선택한 채 **마침**을 누릅니다.

제거하려면 MagicConverter를 종료하고 같은 ZIP의 `magic_converter-uninstall.exe`를 실행합니다. <br>
제거 마법사에서 설치 위치를 확인한 뒤 **제거**와 **마침**을 차례로 누릅니다. <br>
설정과 변환 기록, 원본과 결과 파일은 보존됩니다. 제거가 막히면 화면에 표시된 사유를 해결한 뒤 **다시 시도**를 누릅니다.

[설치 및 업그레이드](docs/user/PUBLIC_WINDOWS.md) · [제거 안내](docs/user/UNINSTALL_WINDOWS.md) · [3.0.5 변경 사항](docs/release/3.0.5.md) · [릴리스 목록](https://github.com/handaeho/MagicConverter/releases)

## macOS에서 시작하기

macOS 15 이상을 지원합니다. Mac의 칩에 맞는 **PKG 설치 마법사**로 사용자 홈의 **Applications** 폴더에 설치하거나 ZIP의 `MagicConverter.app`을 **응용 프로그램**으로 옮기세요. PKG는 관리자 권한 없이 설치·제거합니다. Apple M 계열은 Apple Silicon용입니다.

[Apple Silicon 설치 PKG](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.2/MagicConverter-3.0.5-macOS-arm64.pkg) · [Intel 설치 PKG](https://github.com/handaeho/MagicConverter/releases/download/v3.0.5-macos.2/MagicConverter-3.0.5-macOS-x86_64.pkg) · [ZIP 및 설치 안내](docs/user/MACOS.md) · [Mac 수정 사항](docs/release/3.0.5-macos.2.md)

Finder에서 파일을 먼저 끌고 **Shift**를 누르면 변환 휠이, **Control**을 누르면 빠른 작업 휠이 나타납니다. 원하는 항목 위에 파일을 놓으세요. macOS의 Control+클릭은 보조 클릭이므로 드래그를 시작한 뒤 Control을 누릅니다. 브라우저의 이미지 드래그, 프리셋·휠 편집, 폴더 감시, 결과 기록, 오른쪽 아래 진행창과 메뉴 막대 아이콘을 제공합니다.

FFmpeg·FFprobe, Qt, MuPDF, PDFium, rhwp, HEIC/AVIF 엔진과 기본 대체 글꼴을 앱에 포함합니다. 구형 PPT와 인쇄 설정 PDF용 LibreOffice 엔진도 포함하므로 문서 프로그램·Python·Homebrew를 따로 설치할 필요가 없습니다. Windows의 Microsoft Office 출력과 Mac의 내장 LibreOffice 출력은 사용하는 글꼴과 프로그램에 따라 조판이 달라질 수 있습니다. [지원 조건](docs/user/MACOS.md#변환과-지원-조건)을 확인하세요. 유료 Apple 서명·공증은 포함하지 않습니다.

## CLI로 시작하기

PowerShell에서 설치 폴더를 열고 `magic_converter-cli.exe`를 실행합니다.

```powershell
.\magic_converter-cli.exe --help
.\magic_converter-cli.exe convert --to png "C:\Files\sample.jpg" --output "C:\Files\out"
```

MCP 클라이언트에는 같은 폴더의 `magic_converter-mcp.exe`를 stdio 서버로 등록합니다. 폴더 감시와 로컬 요청은 CLI의 `watch`, `serve`, `send` 명령을 사용합니다.

## 지원 확장자와 변환 가능 목록

현재 소스에 등록된 **82종·95개 확장자 표기**입니다. 입력 확장자 94개와 출력 전용 `.html`을 포함합니다. 대소문자를 구분하지 않으며, 출력은 표의 기본 확장자로 저장합니다. 설치본의 지원 목록은 해당 버전 태그의 README에서 확인할 수 있습니다.

**변환 휠 출력**은 파일 하나를 Shift 휠에 놓았을 때 선택할 수 있는 대상이고, **CLI 추가 출력**은 CLI에서 추가로 선택할 수 있는 대상입니다. 필요한 엔진을 사용할 수 있는 경우의 등록 경로이며, 문서·PDF를 거치는 연결 변환도 포함합니다. 여러 파일을 함께 놓으면 공통 대상만 표시됩니다. 실제 파일의 내용과 출력 형식의 제약은 아래 조건을 따릅니다.

| 분류 | 입력 확장자 | 변환 휠 출력 | CLI 추가 출력 | 조건·특징 |
| --- | --- | --- | --- | --- |
| 이미지 | `.png` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.apng` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | PNG는 기본 이미지·재생 시간 보존 · WebP는 1ms 단위로 반올림하고 기본 이미지 제외 |
| 이미지 | `.jpg` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.jpeg` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.jpe` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.jfif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.webp` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.bmp` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.tiff` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.tif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.heic` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.heif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.avif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.svg` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 지원하는 SVG는 벡터·텍스트 PDF로 저장 · 래스터 이미지 출력도 지원 |
| 동영상·GIF | `.mp4` | `.mp4`, `.gif`, `.mp3`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | FFmpeg 내장 · 오디오 추출에는 음성 트랙 필요 |
| 동영상·GIF | `.mov` | `.mp4`, `.gif`, `.mp3`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | FFmpeg 내장 · 오디오 추출에는 음성 트랙 필요 |
| 동영상·GIF | `.mkv` | `.mp4`, `.gif`, `.mp3`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | FFmpeg 내장 · 오디오 추출에는 음성 트랙 필요 |
| 동영상·GIF | `.webm` | `.mp4`, `.gif`, `.mp3`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | FFmpeg 내장 · 오디오 추출에는 음성 트랙 필요 |
| 동영상·GIF | `.avi` | `.mp4`, `.gif`, `.mp3`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | FFmpeg 내장 · 오디오 추출에는 음성 트랙 필요 |
| 동영상·GIF | `.wmv` | `.mp4`, `.gif`, `.mp3`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | FFmpeg 내장 · 오디오 추출에는 음성 트랙 필요 |
| 동영상·GIF | `.gif` | `.png`, `.webp`, `.mp4`, `.gif`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | — | PNG는 APNG 내용으로 저장 가능 · 애니메이션 보존은 형식별 차이 |
| 오디오 | `.mp3` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.m4a` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.wav` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.flac` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.ogg` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.opus` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.wma` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.aiff` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| 오디오 | `.aif` | `.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.opus`, `.aiff`, `.wma` | `.mp4`, `.webm`, `.mov`, `.mkv`, `.avi`, `.wmv` | FFmpeg 내장 · 동영상 출력에는 정지 화면 추가 |
| PDF | `.pdf` | `.docx`, `.xlsx`, `.jpg`, `.png`, `.epub`, `.txt`, `.md`, `.pptx` (고정 이미지), `.pptx` (편집 복원), `.cbz` | `.pdf`, `.html` | 텍스트·표 추출과 편집 복원은 PDF 내용에 따라 달라짐 |
| 일반 문서 | `.docx` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.hwp`, `.hwpx`, `.fb2` | `.jpg`, `.png` | 본문 중심 · 복잡한 원본 배치는 달라질 수 있음 |
| 일반 문서 | `.md` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.fb2` | `.jpg`, `.png` | 본문 중심 · 복잡한 원본 배치는 달라질 수 있음 |
| 일반 문서 | `.markdown` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.fb2` | `.jpg`, `.png` | 본문 중심 · 복잡한 원본 배치는 달라질 수 있음 |
| 일반 문서 | `.txt` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.fb2` | `.jpg`, `.png` | 본문 중심 · 복잡한 원본 배치는 달라질 수 있음 |
| 일반 문서 | `.rtf` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html` | `.jpg`, `.png` | 본문 중심 · 복잡한 원본 배치는 달라질 수 있음 |
| 일반 문서 | `.rtfd` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html` | `.jpg`, `.png` | RTFD 패키지 폴더 입력 지원 |
| 일반 문서 | `.textclipping` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html` | `.jpg`, `.png` | macOS 텍스트 클리핑 내용 추출 |
| 일반 문서 | `.html` | — | — | 출력 전용 |
| 일반 문서 | `.docm` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.hwp`, `.hwpx` | `.jpg`, `.png` | 표준 DOCX로 정규화 · 매크로/실행 객체 제외 |
| 일반 문서 | `.dotx` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.hwp`, `.hwpx` | `.jpg`, `.png` | 표준 DOCX로 정규화 · 매크로/실행 객체 제외 |
| 일반 문서 | `.dotm` | `.pdf`, `.docx`, `.epub`, `.txt`, `.md`, `.html`, `.hwp`, `.hwpx` | `.jpg`, `.png` | 표준 DOCX로 정규화 · 매크로/실행 객체 제외 |
| 한글 문서 | `.hwp` | `.pdf`, `.docx`, `.txt`, `.hwpx` | `.jpg`, `.png`, `.epub`, `.md`, `.html` | rhwp/문서 모델 사용 |
| 한글 문서 | `.hwpx` | `.pdf`, `.docx`, `.txt`, `.hwp` | `.jpg`, `.png`, `.epub`, `.md`, `.html` | rhwp/문서 모델 사용 |
| 프레젠테이션 | `.ppt` | `.pdf`, `.jpg`, `.png` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 |
| 프레젠테이션 | `.pptx` | `.pdf`, `.jpg`, `.png`, `.pptx`, `.hwp`, `.hwpx` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 · 표준 PPTX로 정규화 가능 |
| 프레젠테이션 | `.pptm` | `.pdf`, `.jpg`, `.png`, `.pptx`, `.hwp`, `.hwpx` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 · 표준 PPTX로 정규화 가능 |
| 프레젠테이션 | `.potx` | `.pdf`, `.jpg`, `.png`, `.pptx`, `.hwp`, `.hwpx` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 · 표준 PPTX로 정규화 가능 |
| 프레젠테이션 | `.potm` | `.pdf`, `.jpg`, `.png`, `.pptx`, `.hwp`, `.hwpx` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 · 표준 PPTX로 정규화 가능 |
| 프레젠테이션 | `.ppsx` | `.pdf`, `.jpg`, `.png`, `.pptx`, `.hwp`, `.hwpx` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 · 표준 PPTX로 정규화 가능 |
| 프레젠테이션 | `.ppsm` | `.pdf`, `.jpg`, `.png`, `.pptx`, `.hwp`, `.hwpx` | — | PDF/페이지 이미지는 PowerPoint 또는 LibreOffice 필요 · 표준 PPTX로 정규화 가능 |
| 표·스프레드시트 | `.xlsm` | `.pdf`, `.hwp`, `.hwpx`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표) | `.jpg`, `.png` | 표준 XLSX로 정규화 · 매크로/실행 객체 제외 |
| 표·스프레드시트 | `.xltx` | `.pdf`, `.hwp`, `.hwpx`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표) | `.jpg`, `.png` | 표준 XLSX로 정규화 · 매크로/실행 객체 제외 |
| 표·스프레드시트 | `.xltm` | `.pdf`, `.hwp`, `.hwpx`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표) | `.jpg`, `.png` | 표준 XLSX로 정규화 · 매크로/실행 객체 제외 |
| 표·스프레드시트 | `.xlsx` | `.pdf`, `.pdf` (Excel 인쇄), `.hwp`, `.hwpx`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표) | `.jpg`, `.png` | Excel 인쇄만 실제 Excel 필요 · 일반 PDF는 기본 엔진 사용 |
| 표·스프레드시트 | `.ods` | `.pdf`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표) | `.jpg`, `.png` | 표 데이터 중심 · 시트/서식 보존 범위는 출력에 따라 다름 |
| 표·스프레드시트 | `.csv` | `.pdf`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표), `.json`, `.jsonl`, `.yaml`, `.xml`, `.tsv` (자료) | `.jpg`, `.png` | 표 변환과 구조화 자료 변환을 각각 제공 |
| 표·스프레드시트 | `.tsv` | `.pdf`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표), `.json`, `.jsonl`, `.yaml`, `.xml`, `.csv` (자료) | `.jpg`, `.png` | 표 변환과 구조화 자료 변환을 각각 제공 |
| 표·스프레드시트 | `.xls` | `.pdf`, `.xlsx`, `.ods`, `.csv` (표), `.tsv` (표) | `.jpg`, `.png` | 표 데이터 중심 · 시트/서식 보존 범위는 출력에 따라 다름 |
| 자막 | `.srt` | `.srt`, `.vtt`, `.txt` | — | SRT/VTT 시간 정보 유지 · TXT는 본문 추출 |
| 자막 | `.vtt` | `.srt`, `.vtt`, `.txt` | — | SRT/VTT 시간 정보 유지 · TXT는 본문 추출 |
| 압축 | `.zip` | `.zip`, `.tar`, `.tar.gz` | — | 내용을 풀어 다시 압축 · GZIP 출력은 .tar.gz |
| 압축 | `.tar` | `.zip`, `.tar`, `.tar.gz` | — | 내용을 풀어 다시 압축 · GZIP 출력은 .tar.gz |
| 압축 | `.gz` | `.zip`, `.tar`, `.tar.gz` | — | 내용을 풀어 다시 압축 · GZIP 출력은 .tar.gz |
| 압축 | `.gzip` | `.zip`, `.tar`, `.tar.gz` | — | 내용을 풀어 다시 압축 · GZIP 출력은 .tar.gz |
| 압축 | `.tar.gz` | `.zip`, `.tar`, `.tar.gz` | — | 내용을 풀어 다시 압축 · GZIP 출력은 .tar.gz |
| 압축 | `.tgz` | `.zip`, `.tar`, `.tar.gz` | — | 내용을 풀어 다시 압축 · GZIP 출력은 .tar.gz |
| 압축 | `.tar.xz` | `.zip`, `.tar`, `.tar.gz` | — | 압축 TAR 입력 · 일반 파일의 내용·수정 시각·권한을 가능한 범위에서 보존 |
| 압축 | `.tar.bz2` | `.zip`, `.tar`, `.tar.gz` | — | 압축 TAR 입력 · 일반 파일의 내용·수정 시각·권한을 가능한 범위에서 보존 |
| 압축 | `.tar.zst` | `.zip`, `.tar`, `.tar.gz` | — | 압축 TAR 입력 · 일반 파일의 내용·수정 시각·권한을 가능한 범위에서 보존 |
| 압축 | `.rar` | `.zip`, `.tar`, `.tar.gz` | — | 압축 해제 가능한 RAR · 압축 방식에 맞는 해제 도구 필요 |
| 구조화 데이터 | `.json` | `.jsonl`, `.yaml`, `.toml`, `.xml`, `.csv` (자료), `.tsv` (자료) | — | 자료형·최상위 구조 제약 적용 |
| 구조화 데이터 | `.jsonl` | `.json`, `.yaml`, `.xml`, `.csv` (자료), `.tsv` (자료) | — | 자료형·최상위 구조 제약 적용 |
| 구조화 데이터 | `.ndjson` | `.json`, `.yaml`, `.xml`, `.csv` (자료), `.tsv` (자료) | — | 자료형·최상위 구조 제약 적용 |
| 구조화 데이터 | `.yaml` | `.json`, `.jsonl`, `.toml`, `.xml`, `.csv` (자료), `.tsv` (자료) | — | 자료형·최상위 구조 제약 적용 |
| 구조화 데이터 | `.yml` | `.json`, `.jsonl`, `.toml`, `.xml`, `.csv` (자료), `.tsv` (자료) | — | 자료형·최상위 구조 제약 적용 |
| 구조화 데이터 | `.toml` | `.json`, `.yaml`, `.xml` | — | 자료형·최상위 구조 제약 적용 |
| 구조화 데이터 | `.xml` | `.json`, `.jsonl`, `.yaml`, `.toml`, `.csv` (자료), `.tsv` (자료) | — | 자료형·최상위 구조 제약 적용 |
| 글꼴 | `.ttf` | `.otf`, `.woff`, `.woff2` | — | 외곽선 종류 유지 · CFF/CFF2 → TTF 제외 |
| 글꼴 | `.otf` | `.ttf`, `.woff`, `.woff2` | — | 외곽선 종류 유지 · CFF/CFF2 → TTF 제외 |
| 글꼴 | `.woff` | `.ttf`, `.otf`, `.woff2` | — | 외곽선 종류 유지 · CFF/CFF2 → TTF 제외 |
| 글꼴 | `.woff2` | `.ttf`, `.otf`, `.woff` | — | 외곽선 종류 유지 · CFF/CFF2 → TTF 제외 |
| 전자책·만화 | `.epub` | `.pdf`, `.docx`, `.jpg`, `.png`, `.epub`, `.txt`, `.md`, `.html`, `.fb2` | — | 본문·장·기본 이미지 중심 · PDF/페이지 이미지는 문서 경유 |
| 전자책·만화 | `.fb2` | `.pdf`, `.docx`, `.jpg`, `.png`, `.epub`, `.txt`, `.md`, `.html`, `.fb2` | — | 본문·장·기본 이미지 중심 · PDF/페이지 이미지는 문서 경유 |
| 전자책·만화 | `.cbz` | `.pdf`, `.epub` | — | 페이지 이미지 만화 → PDF 또는 이미지 중심 EPUB |
| 메일·웹 아카이브 | `.eml` | `.txt`, `.html`, `.zip` (메일·첨부 보관) | — | 본문 추출 · EML ZIP은 원본과 첨부 파일 보관 |
| 메일·웹 아카이브 | `.mhtml` | `.txt`, `.html` | — | 웹 아카이브의 HTML 본문·텍스트 추출 |
| 메일·웹 아카이브 | `.mht` | `.txt`, `.html` | — | 웹 아카이브의 HTML 본문·텍스트 추출 |
| 3D 모델 | `.obj` | `.stl`, `.ply` | — | 삼각형 메시 좌표 중심 · 재질/텍스처 제외 |
| 3D 모델 | `.stl` | `.obj`, `.ply` | — | 삼각형 메시 좌표 중심 · 재질/텍스처 제외 |
| 3D 모델 | `.ply` | `.obj`, `.stl` | — | 삼각형 메시 좌표 중심 · 재질/텍스처 제외 |
| 지리정보 | `.geojson` | `.kml`, `.kmz`, `.gpx` | — | WGS84 기준 · GPX 출력은 점·선·트랙 구간 |
| 지리정보 | `.kml` | `.geojson`, `.kmz`, `.gpx` | — | WGS84 기준 · GPX 출력은 점·선·트랙 구간 |
| 지리정보 | `.kmz` | `.geojson`, `.kml`, `.gpx` | — | WGS84 기준 · GPX 출력은 점·선·트랙 구간 |
| 지리정보 | `.gpx` | `.geojson`, `.kml`, `.kmz` | — | WGS84 기준 · GPX 출력은 점·선·트랙 구간 |

- 같은 확장자도 표에 있으면 재저장·재인코딩·표준 형식 정규화 또는 복사를 지원합니다. `.jpeg`, `.tif`, `.heif`, `.markdown`, `.aif`, `.ndjson`, `.yml`, `.mht`, `.tgz` 등의 입력 별칭도 각각 표에 적었습니다.
- PDF → PPTX는 **고정 이미지**와 **편집 복원**을 제공합니다. 스캔 문서의 글자는 OCR 없이 편집 텍스트로 복원되지 않으며, PDF → XLSX는 검출된 표·텍스트를 추출합니다.
- 구조화 자료의 JSONL 출력에는 최상위 배열이, TOML 출력에는 최상위 객체가 필요합니다. CSV/TSV 자료 출력은 같은 열을 가진 객체 배열에 한정됩니다. XML은 자료형 보존용 컨테이너 또는 XML 문서 모델로 처리합니다.
- 설치 ZIP에는 FFmpeg·FFprobe·rhwp가 포함됩니다. 슬라이드의 PDF/이미지 출력에는 PowerPoint 또는 LibreOffice가, **Excel 인쇄**에는 Microsoft Excel이 필요합니다.
- 형식 변환 외에 모든 파일은 Ctrl 파일 도구의 **ZIP 압축**으로 묶을 수 있습니다. CLI 추가 대상은 `convert --to wav sample.mp4`처럼 선택하며, PDF → PPTX 모드는 `--action pptx_image` 또는 `--action pptx_editable`로 고릅니다.

프로젝트 코드와 [MagicConverter 아이콘](assets/README.md)은 [AGPL-3.0-or-later](LICENSE)로 공개합니다. [외부 구성요소 고지](docs/legal/THIRD_PARTY_NOTICES.md)
