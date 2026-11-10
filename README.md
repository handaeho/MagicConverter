<h1 align="center">
  <img src="assets/readme/hero.svg" alt="MagicConverter — Windows 오프라인 파일 변환기" width="100%">
</h1>

<p align="center">
  <strong>탐색기에서 파일을 끌어 원하는 형식에 놓으세요.</strong><br>
  MagicConverter는 Windows에서 문서, 이미지, PDF, 표와 미디어를 변환하는 오픈소스 도구입니다.
</p>

<p align="center">
  <a href="#휠로-변환하기">휠 데모</a> ·
  <a href="#할-수-있는-일">주요 기능</a> ·
  <a href="#windows-다운로드">Windows 다운로드</a> ·
  <a href="#소스로-시작하기">소스 실행</a> ·
  <a href="#명령줄에서-변환하기">명령줄</a> ·
  <a href="LICENSE">라이선스</a>
</p>

## 휠로 변환하기

<p align="center">
  <img src="assets/readme/demo.gif" alt="샘플 JPG를 드래그해 MagicConverter의 변환 휠에서 PNG를 선택하면 sample_converted.png가 생성됩니다." width="100%">
  <br><sub>실제 휠 화면과 샘플 파일의 변환 결과 · <a href="assets/readme/demo-still.png">정지 화면으로 보기</a></sub>
</p>

1. MagicConverter를 실행한 뒤 탐색기에서 파일을 드래그합니다.
2. <kbd>Shift</kbd>를 누르면 커서 주변에 변환 휠이 나타납니다.
3. 원하는 형식에 파일을 놓습니다. 기본 설정에서는 원본 옆에 결과가 별도 파일로 저장됩니다.

드래그할 때 <kbd>Ctrl</kbd>을 누르면 **파일 도구 휠**이 열립니다. 경로 복사, 메타데이터 제거, ZIP 압축 같은 작업을 고를 수 있습니다.

## 할 수 있는 일

| 작업 | 예 |
| --- | --- |
| **이미지 변환** | JPG → PNG, HEIC → JPG, 이미지 → PDF |
| **PDF와 문서 변환** | PDF → 편집 가능한 PPTX, Markdown → HTML, HWPX → DOCX |
| **Excel 인쇄** | 설치된 Microsoft Excel로 저장된 인쇄 영역과 페이지 설정을 적용해 XLSX → PDF |
| **미디어 변환** | FFmpeg로 동영상 형식 변경, 동영상에서 MP3 추출 |
| **반복 작업** | 명령줄로 여러 파일 변환, 감시 폴더에서 새 파일 처리 |

로컬 파일의 변환은 내 PC에서 처리합니다. 일부 형식의 변환이나 인쇄에 필요한 프로그램은 아래에서 확인할 수 있습니다.

## Windows 다운로드

Windows 10·11 64비트용 실행 파일은 [GitHub Releases](https://github.com/handaeho/MagicConverter/releases)에 배포할 예정입니다. 현재는 아래 방법으로 소스를 실행할 수 있습니다.

실행 파일이 게시되면 릴리스의 **Assets**에서 `magic_converter.exe`를 받아 실행하세요. 설치나 Python 설정 없이 열 수 있으며, 작업 표시줄의 알림 영역에 MagicConverter가 나타납니다. PowerShell에서 사용하려면 같은 릴리스의 `magic_converter-cli.exe`를 받아 `.\magic_converter-cli.exe --help`를 실행하세요.

릴리스 안내에서 파일의 SHA-256과 기능별 필요 프로그램을 확인할 수 있습니다. Excel 인쇄와 Office 자동화에는 해당 프로그램이 설치되어 있어야 합니다.

## 소스로 시작하기

Windows 10 또는 11에 **64비트 Python 3.14**를 설치한 뒤, 저장소 폴더에서 PowerShell을 엽니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/runtime.txt
.\.venv\Scripts\python.exe run_magic_converter.py
```

실행하면 Windows 작업 표시줄의 알림 영역에서 MagicConverter를 찾을 수 있습니다.

<details>
<summary><strong>특정 기능에 필요한 프로그램</strong></summary>

- 동영상과 오디오 변환에는 FFmpeg가 필요합니다.
- Excel 인쇄와 Office 자동화는 설치된 해당 Office 프로그램을 사용합니다.

</details>

## 명령줄에서 변환하기

예를 들어 현재 폴더의 `sample.jpg`를 PNG로 변환해 `out` 폴더에 저장합니다.

```powershell
.\.venv\Scripts\python.exe run_magic_converter.py convert --action image:png .\sample.jpg --output .\out --json
```

사용할 수 있는 명령은 `run_magic_converter.py --help`로 확인할 수 있습니다.

## 라이선스

프로젝트 코드와 [MagicConverter 아이콘](assets/README.md)은 [AGPL-3.0-or-later](LICENSE)로 공개합니다. 한글 문서 템플릿의 Apache-2.0 고지, Filee의 MIT 고지 등은 [외부 구성요소 고지](docs/legal/THIRD_PARTY_NOTICES.md)에 정리했습니다.
