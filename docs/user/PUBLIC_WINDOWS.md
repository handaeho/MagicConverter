# MagicConverter 3.0 설치 안내

1. Windows x64 ZIP을 풀고 `magic_converter-setup.exe`를 실행합니다.
2. 설치 마법사에서 **다음**을 누르고 라이선스 조건을 읽습니다. 동의하면 설치 위치를 확인하거나 **찾아보기**로 다른 상위 폴더를 고릅니다. 3.0.4의 기본 위치는 `%LOCALAPPDATA%\Programs\MagicConverter\3.0.4\magic-converter`입니다.
3. **설치**를 누릅니다. 완료 화면에서 **MagicConverter 실행**을 선택한 채 **마침**을 누르면 앱이 열립니다.

설치본은 Windows 10·11 x64에서 인터넷 연결, 관리자 권한, Python 설치 없이 사용할 수 있습니다. Excel 인쇄와 일부 Office 내보내기는 PC에 설치된 해당 프로그램을 사용합니다.

## 3.0.0~3.0.2에서 업그레이드

실행 중인 MagicConverter를 종료하고 3.0.4를 설치합니다. 기존 3.0 프로필의 설정과 작업 기록을 이어서 사용할 수 있습니다. 이전 버전을 정리할 때는 해당 버전 ZIP의 제거 프로그램을 실행합니다.

## 2.2.39에서 업그레이드

기존 MagicConverter의 변환과 폴더 감시를 종료하고 3.0을 설치합니다. 기존 설정·결과 기록·감시 규칙은 3.0 전용 프로필 `%LOCALAPPDATA%\MagicConverter\3.0\profile`에 한 번 가져옵니다. 감시가 끝난 항목의 기록을 보존하며, 대기 중이던 원본은 다음 감시 시 다시 확인합니다. 기존 시작 메뉴 바로가기와 탐색기 메뉴는 3.0을 가리킵니다.

3.0에서 프리셋과 이전 기록을 확인한 후 2.2.39를 제거할 수 있습니다. 2.2.39 제거 도구는 3.0의 프로필을 제거하지 않습니다. `--data-dir` 또는 `MAGIC_CONVERTER_PROFILE_DIR`로 별도 설정 폴더를 쓰는 경우 같은 값을 3.0에도 지정할 수 있습니다.

## CLI와 MCP

설치 폴더를 열고 PowerShell에서 실행합니다.

```powershell
.\magic_converter-cli.exe --version
.\magic_converter-cli.exe convert --to png "C:\Files\sample.jpg" --output "C:\Files\out"
```

MCP 클라이언트에 `magic_converter-mcp.exe`를 stdio 서버로 등록합니다.

[제거 안내](UNINSTALL_WINDOWS.md)
