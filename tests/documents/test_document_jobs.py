"""Hidden approvals fail promptly; stalled Office jobs cannot starve others."""

import os
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from magic_converter.core.conversion.converter import ConversionError, ConversionService, ConversionWorker
from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.jobs.job_control import Control, JobCancelled, check_cancelled, current
from magic_converter.core.engines.office_process import run_helper

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)




if __name__ == "__main__":
    unittest.main()
