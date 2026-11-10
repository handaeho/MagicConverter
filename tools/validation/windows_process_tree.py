"""Read-only ToolHelp parent identities for validation-owned process trees."""
import ctypes
from ctypes import wintypes as w


def parents():
    # Microsoft PROCESSENTRY32W / TH32CS_SNAPPROCESS contract.
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
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    result={};entry=Entry();entry.dwSize=ctypes.sizeof(entry)
    try:
        success=first(handle,ctypes.byref(entry))
        while success:
            result[entry.pid]=entry.parent;success=next_entry(handle,ctypes.byref(entry))
    finally:close(handle)
    return result


def owned_native_parent(native_pid, root_pid):
    mapping=parents();owner=mapping.get(native_pid);current=owner;seen=set()
    while current and current not in seen:
        if current==root_pid:return owner
        seen.add(current);current=mapping.get(current)
    raise ValueError('Native parent is not in the verifier-launched helper ancestry')
