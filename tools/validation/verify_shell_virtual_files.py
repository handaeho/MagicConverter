"""Real Windows compressed-folder IDataObject, not a fabricated descriptor.

Provider acquisition uses native Shell COM. Qt MIME transport below is an
explicit reconstruction; this is not evidence of an actual OLE drag event.
"""
import argparse,gc,hashlib,io,json,os,struct,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__


def capture(archive):
 import pythoncom
 from win32com.shell import shell,shellcon
 desktop=shell.SHGetDesktopFolder()
 pidl=desktop.ParseDisplayName(0,None,str(archive.resolve()))[1]
 folder=desktop.BindToObject(pidl,None,shell.IID_IShellFolder)
 items=list(folder.EnumObjects(0,shellcon.SHCONTF_NONFOLDERS))
 _,obj=folder.GetUIObjectOf(0,items,pythoncom.IID_IDataObject,0)
 return obj


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
 root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
 report={'status':'running','version':__version__,'provider':'Windows compressed folder IShellFolder.GetUIObjectOf / IDataObject.GetData',
  'ui_drag':'not performed','qt_transport':'reconstructed from real native descriptor/streams; not native QDropEvent evidence','cases':[]}
 def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
 save()
 try:
  if os.name!='nt':raise RuntimeError('Windows native provider required')
  import pythoncom,win32clipboard
  from PIL import Image
  from PySide6.QtCore import QMimeData
  from magic_converter.core.automation.web_inputs import from_mime
  from magic_converter.core.automation.browser_mime import WINDOWS
  stream=io.BytesIO();Image.new('RGB',(96,64),(48,168,96)).save(stream,'PNG');png=stream.getvalue()
  cases=[('single',{'virtual-image.png':png},True),('multiple',{'one.png':png,'two.png':png},True),
    ('empty',{'empty.txt':b''},False),('count-excess',{f'{i}.txt':b'x' for i in range(101)},False)]
  pythoncom.CoInitialize()
  obj=medium=None
  try:
   for label,entries,expected in cases:
    archive=root/(label+'.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
     for name,data in entries.items():z.writestr(name,data)
    before=hashlib.sha256(archive.read_bytes()).hexdigest();obj=capture(archive)
    formats=list(obj.EnumFormatEtc(pythoncom.DATADIR_GET));named={}
    for f in formats:
     try:named[win32clipboard.GetClipboardFormatName(f[0])]=f
     except Exception:pass
    assert 'FileGroupDescriptorW' in named and 'FileContents' in named
    descriptor=obj.GetData((named['FileGroupDescriptorW'][0],None,1,-1,pythoncom.TYMED_HGLOBAL)).data
    assert isinstance(descriptor,bytes)
    mime=QMimeData();mime.setData(WINDOWS.format('FileGroupDescriptorW'),descriptor)
    count=struct.unpack_from('<I',descriptor)[0];contents={};media=[]
    # Refuse counts/empty sizes before requesting a native content allocation.
    admissible=1<=count<=100 and all(struct.unpack_from('<II',descriptor,4+i*592+64)!=(0,0) for i in range(count))
    if admissible:
     for index in range(count):
      record=descriptor[4+index*592:4+(index+1)*592];name=record[72:].decode('utf-16-le').split('\0',1)[0]
      high,low=struct.unpack_from('<II',record,64);size=(high<<32)|low
      assert 0<size<=32*1024**2
      medium=obj.GetData((named['FileContents'][0],None,1,index,pythoncom.TYMED_ISTREAM))
      assert medium.tymed==pythoncom.TYMED_ISTREAM
      content=medium.data.Read(size+1);assert len(content)==size
      assert content==entries[name]
      mime.setData(WINDOWS.format('FileContents')+f';index={index}',content)
      contents[name]=hashlib.sha256(content).hexdigest();media.append(medium.tymed)
    paths,web=from_mime(mime);accepted=bool(paths or web)
    assert accepted is expected,(label,accepted)
    if expected:
     assert tuple(w.placeholder for w in web)==paths and {w.name:w.data for w in web}==entries
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==before
    report['cases'].append({'case':label,'status':'passed','expected_accept':expected,'native_descriptor_bytes':len(descriptor),
      'native_count':count,'native_streams_read':len(contents),'native_tymed':media,'content_sha256':contents,'archive_sha256':before})
    save();print(json.dumps(report['cases'][-1]),flush=True)
  finally:
   medium=obj=None;gc.collect()
   pythoncom.CoUninitialize()
  report['status']='passed'
 except Exception as error:
  import traceback
  report.update(status='failed',error=type(error).__name__+': '+str(error),traceback=traceback.format_exc())
 save();return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
