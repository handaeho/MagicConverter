# Windows 실행 파일 설치

MagicConverter 2.2.28 공개 실행 파일은 Windows 10·11 64비트용입니다. EXE에는 앱 코드와 아이콘, 문서 템플릿이 들어 있습니다. Python과 변환 라이브러리는 처음 한 번 설치합니다.

## 처음 한 번 설치하기

1. [Python 3.14.7](https://www.python.org/downloads/release/python-3147/)에서 Windows 64비트 설치 프로그램을 설치합니다.
2. [GitHub Releases](https://github.com/handaeho/MagicConverter/releases)의 **Assets**에서 `magic_converter.exe`와 `magic_converter-runtime.txt`를 같은 폴더에 받습니다. 명령줄로 쓰려면 `magic_converter-cli.exe`도 같은 곳에 받습니다.
3. 그 폴더에서 PowerShell을 열고 두 줄을 순서대로 실행합니다. 두 번째 줄에서 필요한 라이브러리를 PyPI에서 받아 파일 해시를 확인합니다.

   ```powershell
   py -3.14 -m venv .runtime
   .\.runtime\Scripts\python.exe -m pip install --index-url https://pypi.org/simple --only-binary=:all: --require-hashes -r .\magic_converter-runtime.txt
   ```

4. `magic_converter.exe`를 더블클릭합니다. 앱은 Windows 작업 표시줄의 알림 영역에 나타납니다. PowerShell에서 명령줄 도움말은 `.\magic_converter-cli.exe --help`로 엽니다.

설치가 끝나면 `.runtime` 폴더를 EXE 옆에 둡니다. 설치된 런타임을 다른 곳에서 사용하려면 실행할 PowerShell에서 `MAGIC_CONVERTER_PUBLIC_RUNTIME`에 그 폴더의 절대 경로를 지정할 수 있습니다. 변환은 로컬에서 실행하며 웹 파일을 직접 입력할 때는 연결이 필요합니다.

## 필요한 기능만 추가로 설치하기

| 기능 | 필요한 프로그램 |
| --- | --- |
| 동영상·오디오 변환 | [FFmpeg 다운로드](https://ffmpeg.org/download.html)의 Windows 빌드에서 `ffmpeg.exe`와 `ffprobe.exe`를 함께 준비합니다. 앱 설정에서 `ffmpeg.exe`의 경로를 지정하거나 두 파일이 든 폴더를 PATH에 추가합니다. |
| rhwp로 HWP 저장 또는 PDF 출력 | [rhwp 0.8.7 Windows x64](https://github.com/edwardkim/rhwp/releases/tag/v0.8.7)를 별도 폴더에 압축 해제합니다. 실행할 PowerShell에서 `$env:MAGIC_CONVERTER_RHWP = 'C:\Tools\rhwp\rhwp.exe'`처럼 경로를 지정합니다. 런타임이 필요한 경우 [Microsoft의 공식 설치 프로그램](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)을 사용합니다. |
| Excel 인쇄와 Microsoft Office 자동화 | PC에 설치된 해당 Microsoft Office 프로그램을 사용합니다. |

이미지 변환, 기본 PDF 작업 및 앱 자체 문서 변환은 첫 런타임 설치 후 사용할 수 있습니다. 기능별 보존 범위와 경고는 앱에서 확인할 수 있습니다.

## 다운로드 확인과 소스

다운로드한 EXE의 SHA-256을 같은 릴리스의 `SHA256SUMS.txt`와 비교할 수 있습니다.

```powershell
Get-FileHash -Algorithm SHA256 .\magic_converter.exe
```

`MagicConverter-corresponding-source.zip`에는 EXE 안에 든 앱 소스와 데이터, 런처 소스, 아이콘 원본과 빌드 스크립트가 들어 있습니다. 압축을 풀고 Windows PowerShell에서 `py -3.14 tools/build/build_public_windows.py --output dist/public-windows`를 실행해 다시 빌드할 수 있습니다. 빌드는 Windows의 .NET Framework 4.x C# 컴파일러를 사용합니다. `source-manifest.json`에서 빌드 입력과 앱 소스의 해시를 확인할 수 있습니다.

별도로 설치하는 라이브러리의 버전과 휠 해시는 `magic_converter-runtime.txt`에 있습니다. 주요 라이브러리의 원문은 [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/), [Qt](https://download.qt.io/archive/qt/), [PyMuPDF](https://github.com/pymupdf/PyMuPDF) 및 [MuPDF](https://mupdf.com/releases)에서 확인할 수 있습니다.
