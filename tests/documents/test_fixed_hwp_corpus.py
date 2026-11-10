"""Always-available pinned HWP/HWPX controls; user corpus remains optional."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from magic_converter.core.documents.hwp_documents import read_hwp,read_hwpx,read_docx,write_hwpx,write_docx
from magic_converter.core.documents.rich_hwp import object_counts,walk_blocks,Picture

ROOT=Path(__file__).resolve().parents[2]




if __name__=='__main__':unittest.main()
