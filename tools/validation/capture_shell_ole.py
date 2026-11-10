"""Human Windows ZIP -> Qt OLE capture with exact generated-file scope."""
import argparse,hashlib,io,json,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from PIL import Image
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QApplication,QLabel,QVBoxLayout,QWidget
from magic_converter import __version__
from magic_converter.core.automation.web_inputs import from_mime


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
 root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
 entries={}
 for name,color in [('single.png',(48,168,96)),('pair-a.png',(168,48,96)),('pair-b.png',(96,48,168))]:
  stream=io.BytesIO();Image.new('RGB',(96,64),color).save(stream,'PNG');entries[name]=stream.getvalue()
 archive=root/'native-files.zip'
 with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
  for name,value in entries.items():z.writestr(name,value)
 report={'status':'awaiting_human','version':__version__,'archive':str(archive),'events':[],
  'fixture_sha256':{n:hashlib.sha256(v).hexdigest() for n,v in entries.items()},'session_open':True,
  'limits':'Actual external Qt drop; formats show offered transport, not authenticated supplier PID. Native provider GetData controls are a separate report.'}
 def save():(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 class Receiver(QWidget):
  def __init__(self):
   super().__init__();self.setWindowTitle('Windows ZIP 실제 OLE 수신기');self.setAcceptDrops(True);self.resize(760,380)
   layout=QVBoxLayout(self);layout.addWidget(QLabel('파일 탐색기에서 아래 ZIP을 여세요. 압축을 풀지 마세요.\n'+str(archive)))
   layout.addWidget(QLabel('① single.png 하나를 여기로 드래그\n② pair-a.png·pair-b.png를 Ctrl로 함께 선택해 여기로 드래그\n수신기에는 Shift 없이 드롭합니다. 실제 변환 휠 검사는 따로 Shift를 사용합니다.'))
   self.status=QLabel('드롭 대기 중');self.status.setWordWrap(True);layout.addWidget(self.status)
  def dragEnterEvent(self,event):event.setDropAction(Qt.DropAction.CopyAction);event.accept()
  def dragMoveEvent(self,event):event.setDropAction(Qt.DropAction.CopyAction);event.accept()
  def dropEvent(self,event):
   mime=event.mimeData();formats=list(mime.formats());item={'formats':formats,'external_qt_source':event.source() is None}
   try:
    paths,web=from_mime(mime);values={}
    if web:values={w.name:w.data for w in web}
    else:
     for p in paths:
      if p.stat().st_size>32*1024**2:raise ValueError('Input too large')
      values[p.name]=p.read_bytes()
    case='single' if set(values)=={'single.png'} else 'multiple' if set(values)=={'pair-a.png','pair-b.png'} else 'unscoped'
    matched=case!='unscoped' and all(data==entries[name] for name,data in values.items())
    item.update(case=case,fixture_matches=matched,adapter='web data' if web else 'local paths',
      content_sha256={n:hashlib.sha256(v).hexdigest() for n,v in values.items()},
      virtual_descriptor_offered=any('FileGroupDescriptorW' in f for f in formats),
      virtual_contents_offered=any('FileContents' in f for f in formats))
    if matched:event.setDropAction(Qt.DropAction.CopyAction);event.accept()
    else:event.ignore()
   except Exception as error:item.update(case='error',fixture_matches=False,error=str(error));event.ignore()
   report['events'].append(item);latest={e['case']:e['fixture_matches'] for e in report['events']}
   report['status']='captured_expected_files' if latest.get('single') and latest.get('multiple') else 'incomplete'
   save();self.status.setText('종류: '+item['case']+' / 시험 파일 일치: '+str(item['fixture_matches'])+'\n기술자 제공: '+str(item.get('virtual_descriptor_offered'))+' / 스트림 형식 제공: '+str(item.get('virtual_contents_offered')))
 app=QApplication.instance() or QApplication([]);window=Receiver();window.show();save()
 QTimer.singleShot(30*60*1000,app.quit)
 code=app.exec();report['session_open']=False;save();return code


if __name__=='__main__':raise SystemExit(main())
