"""Real Windows sharing locks during atomic publication."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from magic_converter.core.conversion.converter import output_file
from magic_converter.core.jobs.job_control import Control, JobCancelled, JobTimedOut, current


@unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics required')
class OutputSharingTests(unittest.TestCase):
    def lock(self, path):
        import win32con, win32file
        return win32file.CreateFile(str(path), win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
            win32con.OPEN_EXISTING, 0, None)

    def test_short_source_lock_settles_before_atomic_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'input.txt';source.write_bytes(b'original')
            handle=None;release=None
            try:
                with output_file(source,'.pdf') as (temporary,destination):
                    temporary.write_bytes(b'complete-result')
                    handle=self.lock(temporary)
                    release=threading.Thread(target=lambda: (time.sleep(.2),handle.Close()))
                    release.start()
                self.assertEqual(destination.read_bytes(),b'complete-result')
                self.assertEqual(source.read_bytes(),b'original')
                self.assertEqual(sorted(p.name for p in Path(folder).iterdir()),
                                 sorted([source.name,destination.name]))
            finally:
                if release:release.join()

    def test_publication_wait_keeps_cancellation_and_deadline(self):
        for timed in (False,True):
            with self.subTest(timed=timed),tempfile.TemporaryDirectory() as folder:
                source=Path(folder)/'input.txt';source.write_bytes(b'original')
                event=threading.Event();handle=None
                # The lock is on an existing unrelated target, not the private
                # temporary file, so cancellation cleanup can remove its files.
                other=Path(folder)/'other.pdf';other.write_bytes(b'keep')
                handle=self.lock(other)
                from unittest.mock import patch
                import magic_converter.core.conversion.converter as converter
                replace=converter.os.replace
                trigger=None
                def locked_replace(temporary,destination):
                    return replace(temporary,other)
                token=current.set(Control(event,lambda _:None,time.monotonic()+.12 if timed else None))
                if not timed:
                    trigger=threading.Timer(.12,event.set);trigger.start()
                try:
                    with patch.object(converter.os,'replace',side_effect=locked_replace):
                        with self.assertRaises(JobTimedOut if timed else JobCancelled):
                            with output_file(source,'.pdf') as (temporary,destination):
                                temporary.write_bytes(b'result')
                    self.assertFalse(destination.exists())
                    self.assertEqual(other.read_bytes(),b'keep')
                    self.assertEqual(source.read_bytes(),b'original')
                finally:
                    current.reset(token);handle.Close()
                    if trigger:trigger.join()

    def test_persistent_lock_fails_without_replacing_existing_file(self):
        from unittest.mock import patch
        import magic_converter.core.conversion.converter as converter
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'input.txt';source.write_bytes(b'original')
            other=Path(folder)/'existing.pdf';other.write_bytes(b'existing')
            handle=self.lock(other);replace=converter.os.replace
            started=time.monotonic()
            try:
                with patch.object(converter.os,'replace',side_effect=lambda temporary,destination: replace(temporary,other)):
                    with self.assertRaises(PermissionError):
                        with output_file(source,'.pdf') as (temporary,destination):
                            temporary.write_bytes(b'new')
                self.assertLess(time.monotonic()-started,3)
                self.assertFalse(destination.exists())
                self.assertFalse(temporary.exists())
                self.assertEqual(other.read_bytes(),b'existing')
                self.assertEqual(source.read_bytes(),b'original')
            finally:
                handle.Close()


if __name__ == '__main__':
    unittest.main()
