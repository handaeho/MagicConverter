# Windows 실행 파일 설치

MagicConverter 2.2.29 설치형은 Windows 10·11 64비트용입니다. 설치 EXE 하나로 앱과 필수 실행 환경을 준비합니다.

## 처음 한 번 설치하기

1. 릴리스의 **Assets**에서 `MagicConverter-Setup.exe`를 받고 실행합니다. [설치형 2.2.29](https://github.com/handaeho/MagicConverter/releases/tag/v2.2.29)
2. 설치 확인 창에서 **예**를 누릅니다. 인터넷을 통해 Python 3.14.10과 필수 라이브러리 34개를 받아 해시를 검증하고 현재 Windows 계정의 앱 전용 폴더에 설치합니다.
3. 설치가 완료되면 앱이 열립니다. 이후에는 Windows 시작 메뉴에서 **MagicConverter**를 실행합니다.

설치에는 관리자 권한이 필요하지 않습니다. 설치 폴더는 `%LOCALAPPDATA%\Programs\MagicConverter\2.2.29`입니다. 명령줄 도구도 여기에 설치됩니다.

```powershell
& "$env:LOCALAPPDATA\Programs\MagicConverter\2.2.29\magic_converter-cli.exe" --help
```

설치 오류는 `%LOCALAPPDATA%\Programs\MagicConverter\setup-2.2.29.log`에서 확인할 수 있습니다. 설치 파일과 앱 데이터를 제거하는 방법은 [Windows 제거 안내](UNINSTALL_WINDOWS.md)를 참고하세요.

## 설치 위치 선택 검증본

저장소의 `master` 소스 폴더에서 아래 명령을 실행하면 경로 선택을 포함한 설치 EXE를 빌드할 수 있습니다.

```powershell
py -3.14 tools/build/build_public_windows.py --output dist/public-windows
```

경로 선택이 추가된 로컬 설치 검증본에서는 **설치 안내 → 라이선스 동의 → 설치 폴더 선택 → 최종 확인 → 설치** 순서로 진행합니다. 기본 경로는 `%LOCALAPPDATA%\Programs\MagicConverter\2.2.29`입니다. 다른 로컬 디스크의 새 폴더나 비어 있는 전용 폴더도 선택할 수 있습니다.

설치 후에는 시작 메뉴에서 같은 위치의 앱이 열립니다. 설치 프로그램을 다시 실행하면 등록된 앱을 엽니다. 폴더를 바꾸려면 제거한 뒤 다시 설치하세요. 제거 도구는 선택한 경로와 해당 폴더의 설치 표식을 대조합니다.

기존 2.2.28은 `.runtime`을 수동으로 준비하는 실행형입니다. [실행형 2.2.28과 설치 안내](https://github.com/handaeho/MagicConverter/releases/tag/v2.2.28)

## 인터넷이 없는 PC에 설치하기

1. 인터넷이 연결된 Windows PC에서 `MagicConverter-Setup.exe`를 쓰기 가능한 폴더에 받습니다. 같은 폴더에서 PowerShell을 열고 아래 명령을 실행한 뒤 자료 만들기에 동의하세요.

   ```powershell
   .\MagicConverter-Setup.exe --prepare-offline
   ```

2. 준비가 끝나면 `MagicConverter-Setup.exe`와 생성된 `MagicConverter-offline-runtime` 폴더를 함께 인터넷이 없는 PC의 같은 폴더로 옮깁니다.
3. 그 PC에서 설치 EXE를 더블클릭하고 **예**를 누릅니다. EXE 옆의 폴더를 찾아 Python과 라이브러리의 해시를 확인하고 로컬 파일로 설치합니다.

오프라인 자료는 같은 2.2.29 Windows 64비트 설치 EXE와 함께 사용합니다. 자료 준비에 실패하면 준비 폴더의 `MagicConverter-offline-setup.log`를 확인하세요.

## 필요한 기능만 추가로 설치하기

| 기능 | 필요한 프로그램 |
| --- | --- |
| 동영상·오디오 변환 | [FFmpeg 다운로드](https://ffmpeg.org/download.html)의 Windows 빌드에서 `ffmpeg.exe`와 `ffprobe.exe`를 함께 준비합니다. 앱 설정에서 `ffmpeg.exe`의 경로를 지정하거나 두 파일이 든 폴더를 PATH에 추가합니다. |
| rhwp로 HWP 저장 또는 PDF 출력 | [rhwp 0.8.7 Windows x64](https://github.com/edwardkim/rhwp/releases/tag/v0.8.7)를 별도 폴더에 압축 해제합니다. 실행할 PowerShell에서 `$env:MAGIC_CONVERTER_RHWP = 'C:\Tools\rhwp\rhwp.exe'`처럼 경로를 지정합니다. 런타임이 필요한 경우 [Microsoft의 공식 설치 프로그램](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)을 사용합니다. |
| Excel 인쇄와 Microsoft Office 자동화 | PC에 설치된 해당 Microsoft Office 프로그램을 사용합니다. |

이미지 변환, 기본 PDF 작업 및 앱 자체 문서 변환은 첫 런타임 설치 후 사용할 수 있습니다. 기능별 보존 범위와 경고는 앱에서 확인할 수 있습니다.

## 다운로드 확인과 소스

다운로드한 설치 EXE의 SHA-256을 같은 릴리스의 `SHA256SUMS.txt`와 비교할 수 있습니다.

```powershell
Get-FileHash -Algorithm SHA256 .\MagicConverter-Setup.exe
```

`MagicConverter-corresponding-source.zip`에는 앱 소스와 데이터, 설치 프로그램과 런처 소스, 아이콘 원본 및 빌드 스크립트가 들어 있습니다. 압축을 풀고 Windows PowerShell에서 `py -3.14 tools/build/build_public_windows.py --output dist/public-windows`를 실행해 다시 빌드할 수 있습니다. 빌드는 Windows의 .NET Framework 4.x C# 컴파일러를 사용합니다. `source-manifest.json`에서 빌드 입력과 설치 EXE에 담긴 파일의 해시를 확인할 수 있습니다.

자동 설치되는 라이브러리의 버전과 휠 해시는 설치 폴더와 대응 소스 ZIP의 `magic_converter-runtime.txt`에 있습니다. 설치 프로그램이 받는 Python과 pip의 주소 및 해시는 `packaging/public-windows/setup.cs`에 있습니다. [Python 3.14.10](https://www.python.org/downloads/release/python-31410/) · [pip 26.3](https://pypi.org/project/pip/26.3/)

주요 라이브러리 원문은 각 프로젝트에서 확인할 수 있습니다. [Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/) · [Qt](https://download.qt.io/archive/qt/) · [PyMuPDF](https://github.com/pymupdf/PyMuPDF) · [MuPDF](https://mupdf.com/releases)
