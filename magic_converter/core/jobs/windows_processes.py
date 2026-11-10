"""Read process names/parents without opening another process's memory."""
import ctypes
from ctypes import wintypes as w


def process_snapshot():
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',w.DWORD),('cntUsage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),
            ('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),
            ('flags',w.DWORD),('image',w.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    snapshot=kernel.CreateToolhelp32Snapshot;snapshot.argtypes=[w.DWORD,w.DWORD];snapshot.restype=w.HANDLE
    first=kernel.Process32FirstW;next_entry=kernel.Process32NextW
    for function in (first,next_entry):function.argtypes=[w.HANDLE,ctypes.POINTER(Entry)];function.restype=w.BOOL
    close=kernel.CloseHandle;close.argtypes=[w.HANDLE];close.restype=w.BOOL
    handle=snapshot(2,0)
    if not handle or handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    result={};entry=Entry();entry.dwSize=ctypes.sizeof(entry)
    try:
        success=first(handle,ctypes.byref(entry))
        while success:
            result[entry.pid]={'parent':entry.parent,'image':entry.image}
            success=next_entry(handle,ctypes.byref(entry))
        # An incomplete snapshot cannot prove that a user server is absent.
        error=ctypes.get_last_error()
        if error!=18:raise ctypes.WinError(error)  # ERROR_NO_MORE_FILES
    finally:close(handle)
    return result
