<h1 align="center">
  <img src="assets/readme/hero.svg" alt="MagicConverter — Windows 파일 변환기" width="100%">
</h1>

탐색기에서 파일을 끌어 즉시 문서, PDF, 이미지, 표와 미디어를 변환하는 Windows 앱입니다.

[EXE로 시작하기](#exe로-시작하기) · [CLI로 시작하기](#cli로-시작하기)

## EXE로 시작하기

ZIP에는 다음 실행 프로그램이 포함됩니다.

- [MagicConverter 2.2.34 Windows ZIP](https://github.com/handaeho/MagicConverter/releases/download/v2.2.34/MagicConverter-2.2.34-Windows-x64.zip)
- 설치 마법사 `MagicConverter-Setup.exe`
- 제거 마법사 `Uninstall.exe`

압축을 푼 뒤 설치 마법사를 실행합니다. 

- Windows 10·11 64비트용입니다.
- 설치 마법사가 내장된 Python, 필수 라이브러리, FFmpeg·FFprobe와 rhwp를 설치합니다.
- 인터넷 연결 없이 설치할 수 있습니다.

1. 설치 안내를 읽고 라이선스에 동의합니다.
2. 설치 폴더를 고르고 확인한 뒤 **설치**를 누릅니다.
3. 앱이 열리면 탐색기에서 파일을 드래그하고 <kbd>Shift</kbd>를 눌러 변환 형식을 선택합니다. <kbd>Ctrl</kbd>을 누르면 파일 도구 휠이 열립니다.

[설치 안내](docs/user/PUBLIC_WINDOWS.md) · [제거 안내](docs/user/UNINSTALL_WINDOWS.md) · [릴리스와 대응 소스](https://github.com/handaeho/MagicConverter/releases/tag/v2.2.34)

<p align="center">
  <img src="assets/readme/demo.gif" alt="JPG를 휠로 끌어 PNG로 변환하는 예" width="100%">
  <br><sub><a href="assets/readme/demo-still.png">정지 화면으로 보기</a></sub>
</p>

## CLI로 시작하기

PowerShell에서 선택한 설치 폴더를 열고 설치된 Python으로 `run_magic_converter.py`를 실행합니다.

```powershell
.\runtime\python.exe .\app\run_magic_converter.py --help
.\runtime\python.exe .\app\run_magic_converter.py convert --to png "C:\Files\sample.jpg" --output "C:\Files\out"
```

현재는 Python 파일을 실행하는 방식입니다. <br> **MCP 전환은 후속 버전에서 공개**할 예정입니다.

## 지원 확장자와 변환 가능 목록

현재 지원하는 **79종·89개 확장자 표기** 전체입니다. <br> 입력 확장자 88개와 출력 전용 `.html`을 포함합니다. <br> 대소문자를 구분하지 않으며, 출력은 표의 기본 확장자로 저장합니다.

**변환 휠 출력**은 파일 하나를 Shift 휠에 놓았을 때 선택할 수 있는 대상이고, **CLI 추가 출력**은 CLI에서 추가로 선택할 수 있는 대상입니다. <br> 필요한 엔진을 사용할 수 있는 경우의 등록 경로이며, 문서·PDF를 거치는 연결 변환도 포함합니다. <br> 여러 파일을 함께 놓으면 공통 대상만 표시됩니다. <br> 실제 파일의 내용과 출력 형식의 제약은 아래 조건을 따릅니다.

| 분류 | 입력 확장자 | 변환 휠 출력 | CLI 추가 출력 | 조건·특징 |
| --- | --- | --- | --- | --- |
| 이미지 | `.png` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.jpg` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.jpeg` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.webp` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.bmp` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.tiff` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.tif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.heic` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.heif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.avif` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | 투명도·다중 프레임 보존은 출력 형식에 따라 다름 |
| 이미지 | `.svg` | `.png`, `.jpg`, `.webp`, `.bmp`, `.tiff`, `.heic`, `.avif`, `.pdf` | — | SVG를 래스터 이미지 또는 PDF로 렌더링 |
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
- 형식 변환 외에 모든 파일은 Ctrl 파일 도구의 **ZIP 압축**으로 묶을 수 있습니다. CLI 추가 대상은 `convert --to wav sample.mp4`처럼 선택하며, PDF → PPTX 모드는 `--action pptx_image` 또는 `--action pptx_editable`로 고릅니다.

프로젝트 코드와 [MagicConverter 아이콘](assets/README.md)은 [AGPL-3.0-or-later](LICENSE)로 공개합니다. <br> [외부 구성요소 고지](docs/legal/THIRD_PARTY_NOTICES.md)
