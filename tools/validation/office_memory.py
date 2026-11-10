"""Observe only Office servers named by this run's identity-checked ledgers."""
import json
from pathlib import Path
import time
import win32api
import win32event
import win32process


class OfficeMemory:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.before=set(win32process.EnumProcesses())
        self.handles={};self.records={};self.peak_rss=0;self.peak_private=0

    def sample(self, job_pids=()):
        for path in self.directory.rglob('*.processes.json'):
            try:
                if path.is_symlink() or path.stat().st_size>65536:continue
                ledger=json.loads(path.read_text(encoding='utf-8'))
                for item in ledger.get('owned',[]):
                    pid=item['pid'];created=item['created'];key=(pid,created)
                    if key in self.handles:
                        if item.get('commit_limit_bytes'):
                            self.records[key]['native_commit_limit_bytes']=item['commit_limit_bytes']
                        for name in ('budget_phase','activation','application','preactivation_bounded','print_queue_sha256'):
                            if name in item:self.records[key][name]=item[name]
                        continue
                    if pid in self.before:continue
                    handle=win32api.OpenProcess(0x100410,False,pid)
                    try:
                        matches=str(win32process.GetProcessTimes(handle)['CreationTime'])==created
                    except BaseException:
                        handle.Close();raise
                    if not matches:
                        handle.Close();continue
                    try:image=win32process.GetModuleFileNameEx(handle,0)
                    except win32api.error:image=None
                    self.handles[key]=handle
                    self.records[key]={'pid':pid,'created':created,'ledger':str(path),
                        'process_image':image,
                        'native_commit_limit_bytes':item.get('commit_limit_bytes'),
                        **{name:item[name] for name in ('budget_phase','activation','application','preactivation_bounded','print_queue_sha256') if name in item},
                        'peak_rss_bytes':0,'peak_private_bytes':0,'in_observed_job':False}
            except (OSError,ValueError,KeyError,TypeError,win32api.error):
                continue  # Atomic ledger replacement / process exit can race observation.
        rss=private=0
        for key,handle in self.handles.items():
            try:
                memory=win32process.GetProcessMemoryInfo(handle);record=self.records[key]
                record['peak_rss_bytes']=max(record['peak_rss_bytes'],memory['WorkingSetSize'])
                committed=memory.get('PrivateUsage',memory['PagefileUsage'])
                record['peak_private_bytes']=max(record['peak_private_bytes'],committed)
                record['in_observed_job'] |= key[0] in job_pids
                if key[0] not in job_pids:
                    rss+=memory['WorkingSetSize'];private+=committed
            except win32api.error:pass
        self.peak_rss=max(self.peak_rss,rss);self.peak_private=max(self.peak_private,private)
        return rss

    def finish(self):
        started=time.monotonic()
        while True:
            live=[key[0] for key,h in self.handles.items() if win32event.WaitForSingleObject(h,0)==258]
            if not live or time.monotonic()-started>=3:break
            time.sleep(.025)
        return {'measurement':'25ms samples; only new PID/CreationTime pairs from this run\'s native ledgers. Pre-existing/reused servers excluded.',
                'external_peak_rss_bytes':self.peak_rss,'external_peak_private_bytes':self.peak_private,
                'processes':list(self.records.values()),'lingering_before_verifier_cleanup':live}

    def close(self):
        for handle in self.handles.values():handle.Close()
