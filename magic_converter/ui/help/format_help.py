"""User-facing support matrix including fidelity and engine requirements."""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout, QWidget


class FormatHelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("magic_converter · 지원 형식")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        text = QTextBrowser()
        text.setHtml("""
        <h2>지원 형식 및 출력 방식</h2>
        <table border="1" cellpadding="7" cellspacing="0">
        <tr><th>입력</th><th>출력</th></tr>
        <tr><td>이미지: JPG, PNG, WebP, HEIC/HEIF, TIFF, AVIF, BMP, SVG</td><td>JPG, PNG, WebP, HEIC, TIFF, AVIF, BMP, PDF<br>SVG는 입력만 · 50% 압축은 품질/크기 조정</td></tr>
        <tr><td>오디오: MP3, M4A, WAV, FLAC, OGG, Opus, AIFF, WMA</td><td>같은 오디오 형식 사이 변환</td></tr>
        <tr><td>영상: MP4, MOV, MKV, WebM, AVI, WMV, GIF</td><td>같은 영상 형식 사이 변환, MP3 추출</td></tr>
        <tr><td>PDF</td><td>DOCX, PPTX (고정), PPTX (편집), XLSX, JPG, PNG, EPUB, TXT, MD</td></tr>
        <tr><td>문서: DOCX, MD, TXT, EPUB, RTF, RTFD, textClipping</td><td>DOCX, MD, TXT, EPUB, HTML, PDF</td></tr>
        <tr><td>한글: HWP / HWPX</td><td>DOCX, PDF, TXT, HWP ↔ HWPX</td></tr>
        <tr><td>DOCX / PPTX / XLSX</td><td>HWP · DOCX는 HWPX도 지원<br>HWP 저장에는 rhwp 필요</td></tr>
        <tr><td>슬라이드: PPT/PPTX</td><td>PDF, JPG, PNG · PowerPoint 또는 LibreOffice 필요</td></tr>
        <tr><td>표: XLSX, ODS, CSV, TSV, XLS</td><td>XLSX, ODS, CSV, TSV, PDF · XLS는 입력만</td></tr>
        <tr><td>자막: SRT, VTT</td><td>SRT, VTT, TXT</td></tr>
        <tr><td>압축: ZIP, TAR, GZIP, RAR</td><td>ZIP, TAR, tar.gz · RAR은 입력만</td></tr>
        </table>
        <h3>결과를 확인할 때</h3>
        <ul>
        <li>PDF와 슬라이드 이미지는 <b>모든 페이지를 300 DPI</b>로 새 폴더에 출력합니다.</li>
        <li><b>PPTX (고정)</b>: 페이지를 200 DPI 이미지로 유지합니다. 출력은 <b>원본이름_slide.pptx</b>입니다.</li>
        <li><b>PPTX (편집)</b>: 검출된 표의 실제 PowerPoint 셀·병합 복원, 나머지 본문의 편집 가능한 텍스트 상자 생성. 표 영역의 배경 제거로 이동 후 잔상 방지. 출력: <b>원본이름_editable.pptx</b>.</li>
        <li>미검출 표·도형·표 밖 이미지는 150 DPI 배경 유지. 표 내부 그림은 별도 그림 개체로 보존. 테두리 없는 표·복잡한 중첩 표·글꼴 대체에 따른 복원 한계.</li>
        <li>PDF → DOCX: 본문 스타일·검출된 표·병합 셀·그림 보존. 쪽 크기 유지 및 읽기 순서 재구성. 복잡한 다단·떠 있는 개체의 배치 차이 가능.</li>
        <li>스캔 PDF: DOCX·편집형 PPTX 모두 이미지 유지. 표·글자 편집에는 OCR 및 표 구조 인식 필요. 현재 OCR 미포함.</li>
        <li>PDF → XLSX는 표가 있으면 셀로, 없으면 페이지 텍스트를 행으로 추출합니다. OCR은 포함하지 않습니다.</li>
        <li>문서/표 상호 변환은 내용 중심입니다. 복잡한 레이아웃, 이미지, 차트, 수식 결과가 그대로 보존되지는 않습니다.</li>
        <li>문서·Office 변환은 별도 작업 큐에서 실행합니다. 파일 열기 30초, 저장 90초, 전체 180초 제한으로 응답을 감시하며, 트레이의 <b>진행/대기 변환 모두 취소</b>로 작업을 중단할 수 있습니다.</li>
        <li>한글 문서는 rhwp와 문서 모델로 변환합니다. 글꼴 대체·수식·떠 있는 개체 위치·페이지 나눔은 원본과 다를 수 있습니다. 읽지 못한 표·그림을 조용히 누락시키지 않고 오류로 안내합니다. PPTX/XLSX → HWP는 텍스트·표를 추출합니다.</li>
        <li>DOCX/PPTX/XLSX → PDF는 설치된 Word/PowerPoint/Excel COM을 우선 사용합니다. Office 응답 제한은 180초입니다.</li>
        <li>CSV/TSV를 여러 시트에서 내보내면 모든 시트를 새 폴더에 각각 저장합니다.</li>
        <li>자막 스타일·화면 위치는 보존하지 않습니다. RAR 암호/솔리드/분할 압축은 별도 RAR 엔진이 필요할 수 있습니다.</li>
        <li>RTFD는 본문 RTF를 읽습니다. textClipping은 텍스트 plist만 지원하며 macOS 리소스 포크만 있는 파일은 변환할 수 없습니다.</li>
        </ul>
        """)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
