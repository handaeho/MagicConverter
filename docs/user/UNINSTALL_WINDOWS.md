# MagicConverter 3.0 제거

1. 변환과 폴더 감시를 마친 뒤 알림 영역에서 MagicConverter를 종료합니다.
2. 설치 ZIP을 풀어 설치 폴더 밖에서 `magic_converter-uninstall.exe`를 실행합니다.
3. 기록된 실제 설치 위치를 확인하고 제거에 동의합니다.

제거 도구는 설치 기록과 일치하는 프로그램 파일, 시작 메뉴 바로가기, 탐색기 메뉴를 제거합니다. 사용자 설정·기록, 원본, 변환 결과, 설치 폴더에 추가한 파일을 보존합니다.

PowerShell에서 무인으로 제거하려면 `.\magic_converter-uninstall.exe uninstall --quiet`를 실행합니다. 필요하면 `--parent "D:\Apps"`로 설치 시 선택한 상위 폴더를 직접 지정할 수 있습니다.

[설치 안내](PUBLIC_WINDOWS.md)
