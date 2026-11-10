"""Commit limits for newly created, identity-checked native COM servers.

SCM-launched Office is outside the helper's process tree. Keep this separate
Job handle in the helper so even abrupt helper termination closes the native
server Job. No process-name matching or existing-server attachment is allowed.
"""
from __future__ import annotations

OFFICE_COMMIT_BYTES = 2 * 1024 ** 3
NATIVE_EXIT_GRACE_SECONDS = 1.0


class OfficeProcessBudget:
    def __init__(self, before, memory_limit=OFFICE_COMMIT_BYTES):
        if memory_limit <= 0:
            raise ValueError('A positive native commit budget is required')
        self.before = frozenset(before)
        self.memory_limit = memory_limit
        self.job = None
        self.identities = set()

    def attach(self, pid: int, created: str) -> bool:
        import win32api
        import win32job
        import win32process
        if pid in self.before:
            return False
        if (pid, created) in self.identities:
            return True
        # SET_QUOTA + TERMINATE are required by AssignProcessToJobObject.
        handle = win32api.OpenProcess(0x0400 | 0x0100 | 0x0001, False, pid)
        try:
            if str(win32process.GetProcessTimes(handle)['CreationTime']) != created:
                return False
            if self.job is None:
                job = win32job.CreateJobObject(None, '')
                try:
                    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                    limits['BasicLimitInformation']['LimitFlags'] |= (
                        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                        win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY)
                    limits['ProcessMemoryLimit'] = limits['JobMemoryLimit'] = self.memory_limit
                    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
                except BaseException:
                    job.Close()
                    raise
                self.job = job
            # Do not suppress failure and silently run a supposedly bounded
            # owned server without the requested OS allocation limit.
            win32job.AssignProcessToJobObject(self.job, handle)
            self.identities.add((pid, created))
            return True
        finally:
            handle.Close()

    def close(self):
        if self.job is not None:
            self.job.Close()
            self.job = None

    def wait_for_exit(self, identities, seconds=NATIVE_EXIT_GRACE_SECONDS):
        """Allow successful Quit to finish while its existing Job stays active.

        A single deadline covers all servers. This cannot grant ownership or
        wait on an arbitrary PID: attachment and creation time must both match.
        Failure/crash cleanup still closes the Job immediately.
        """
        import math
        import time
        import win32api
        import win32event
        import win32process
        import pywintypes
        if not math.isfinite(seconds) or not 0 <= seconds <= NATIVE_EXIT_GRACE_SECONDS:
            raise ValueError('Native exit grace must be between zero and one second')
        started = time.monotonic()
        deadline = started + seconds
        results = []
        for pid, created in sorted(set(identities)):
            item = {'pid': pid, 'created': created, 'status': 'not_attached'}
            results.append(item)
            if self.job is None or (pid, created) not in self.identities:
                continue
            try:
                handle = win32api.OpenProcess(0x00100000 | 0x0400, False, pid)
                try:
                    if str(win32process.GetProcessTimes(handle)['CreationTime']) != created:
                        item['status'] = 'identity_changed'
                        continue
                    remaining = max(0, int((deadline - time.monotonic()) * 1000))
                    result = win32event.WaitForSingleObject(handle, remaining)
                    item['status'] = 'exited' if result == win32event.WAIT_OBJECT_0 else 'timeout'
                finally:
                    handle.Close()
            except pywintypes.error as exc:
                # ERROR_INVALID_PARAMETER is the normal already-exited case.
                item['status'] = 'gone' if exc.winerror == 87 else 'unavailable'
                item['winerror'] = exc.winerror
        return {'limit_seconds': seconds, 'seconds': time.monotonic() - started, 'processes': results}
