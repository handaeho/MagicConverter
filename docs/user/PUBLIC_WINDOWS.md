# MagicConverter 2.2.32 설치 안내

## EXE로 시작하기

1. ZIP을 다운로드해 압축을 푼 뒤 `MagicConverter-Setup.exe`를 실행합니다. [Windows ZIP](https://github.com/handaeho/MagicConverter/releases/download/v2.2.32/MagicConverter-2.2.32-Windows-x64.zip)
2. 설치 안내를 읽고 라이선스에 동의한 뒤 상위 폴더를 선택합니다. 예를 들어 `D:\Apps`를 선택하면 `D:\Apps\magic-converter`를 새로 만듭니다. 상위 폴더에는 기존 파일이 있어도 됩니다. 같은 이름의 파일이나 폴더가 있으면 다른 상위 폴더를 선택하세요. 기본 설치 위치는 `%LOCALAPPDATA%\Programs\MagicConverter\2.2.32\magic-converter`입니다.
3. 경로를 확인하고 **설치**를 누릅니다. 마법사가 내장 파일의 해시를 확인한 뒤 Python 3.14.10, 필수 라이브러리 34개, FFmpeg·FFprobe와 rhwp를 설치합니다. 설치 후 앱이 열리고 시작 메뉴에서 다시 실행할 수 있습니다.

Windows 10·11 64비트에서 같은 설치 파일을 사용합니다. 인터넷 연결이나 관리자 권한 없이 설치할 수 있습니다. 설치 오류는 `%LOCALAPPDATA%\Programs\MagicConverter\setup-2.2.32.log`에서 확인할 수 있습니다. [제거 안내](UNINSTALL_WINDOWS.md)




## CLI로 시작하기

PowerShell에서 선택한 설치 폴더를 열고 설치된 Python으로 `run_magic_converter.py`를 실행합니다.

```powershell
.\runtime\python.exe .\app\run_magic_converter.py --help
.\runtime\python.exe .\app\run_magic_converter.py convert --to png "C:\Files\sample.jpg" --output "C:\Files\out"
```

현재는 Python 파일을 실행하는 방식입니다. MCP 전환은 후속 버전에서 진행할 예정입니다.
