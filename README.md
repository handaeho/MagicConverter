# MagicConverter

Windows에서 이미지, 문서, PDF, 표와 미디어를 변환하는 도구입니다. 시스템 트레이 메뉴와 명령줄을 제공합니다. 이 저장소에는 프로젝트 소스와 사용 안내가 들어 있습니다.

## 소스에서 실행

64비트 Python 3.14가 설치된 Windows PowerShell에서 실행합니다.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements/runtime.txt
.\.venv\Scripts\python.exe run_magic_converter.py
```

명령줄에서 명령 목록을 보거나 변환할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe run_magic_converter.py --help
.\.venv\Scripts\python.exe run_magic_converter.py convert --action document:html .\example.md --output .\out --json
```


## 라이선스

프로젝트 코드와 [MagicConverter 아이콘](assets/README.md)은 [AGPL-3.0-or-later](LICENSE)입니다. 빈 HWPX 템플릿은 Apache-2.0으로 제공되며 템플릿의 원래 라이선스와 고지 파일을 포함합니다. Filee의 MIT 고지와 기타 외부 구성요소의 조건은 [외부 구성요소 고지](docs/legal/THIRD_PARTY_NOTICES.md)에 기록했습니다.
