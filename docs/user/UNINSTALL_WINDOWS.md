# MagicConverter 2.2.30 제거

1. 실행 중인 변환을 마치고 알림 영역에서 MagicConverter를 종료합니다. CLI나 폴더 감시를 사용 중이면 함께 종료합니다.
2. [제거 EXE](https://github.com/handaeho/MagicConverter/releases/download/v2.2.30/Uninstall.exe)를 받고 다운로드 폴더처럼 설치 폴더 밖에서 실행합니다.
3. 확인 창에 표시된 설치 위치와 제거 항목을 확인하고 **예**를 누릅니다.

설치 프로그램은 선택한 위치를 기록합니다. 제거 도구는 그 기록과 설치 폴더의 표식이 일치하는지 확인합니다.



이 제거 도구는 관리자 권한이나 인터넷 연결 없이 실행됩니다. 파일이 사용 중이라는 안내가 나오면 해당 앱을 종료한 뒤 다시 실행하세요.

## 소스에서 빌드하기

저장소의 `master`를 받거나 `MagicConverter-corresponding-source.zip`을 풉니다. 소스 폴더의 PowerShell에서 `py -3.14 tools/build/build_windows_uninstaller.py --output dist/uninstall`을 실행합니다. ZIP의 `packaging/public-windows/uninstall-files.txt`는 검증된 2.2.30 설치 파일의 상대 경로 목록입니다. 바이너리의 SHA-256은 `SHA256SUMS.txt`에서 확인할 수 있습니다.
