# MagicConverter 2.2.30 사용 안내

## EXE로 시작하기

1. [설치 EXE](https://github.com/handaeho/MagicConverter/releases/download/v2.2.30/MagicConverter-Setup.exe)를 다운로드해 실행합니다.
2. 설치 안내를 읽고 라이선스에 동의한 뒤 설치 폴더를 선택합니다. 기본 폴더는 `%LOCALAPPDATA%\Programs\MagicConverter\2.2.30`입니다. 새 폴더나 비어 있는 전용 폴더를 지정할 수도 있습니다.
3. 경로를 확인하고 **설치**를 누릅니다. 마법사가 Python 3.14.10과 필수 라이브러리 34개를 내려받아 해시를 확인한 뒤 설치합니다. 설치 후 앱이 열리고 시작 메뉴에서 다시 실행할 수 있습니다.

Windows 10·11 64비트에서 같은 설치 파일을 사용합니다. 설치할 때 인터넷 연결이 필요하며, 관리자 권한 없이 설치할 수 있습니다. 설치 오류는 `%LOCALAPPDATA%\Programs\MagicConverter\setup-2.2.30.log`에서 확인할 수 있습니다. [제거 안내](UNINSTALL_WINDOWS.md)


[릴리스](https://github.com/handaeho/MagicConverter/releases/tag/v2.2.30)에서 설치 파일의 SHA-256, 라이선스 고지와 대응 소스를 확인할 수 있습니다.

## CLI로 시작하기

PowerShell에서 선택한 설치 폴더를 열고 설치된 Python으로 `run_magic_converter.py`를 실행합니다.

```powershell
.\runtime\python.exe .\app\run_magic_converter.py --help
.\runtime\python.exe .\app\run_magic_converter.py convert --to png "C:\Files\sample.jpg" --output "C:\Files\out"
```

현재는 Python 파일을 실행하는 방식입니다. MCP 전환은 후속 버전에서 진행할 예정입니다.
