import subprocess
import unittest
from unittest.mock import patch

from tools.validation.validation_environment import check_environment


class ValidationEnvironmentTests(unittest.TestCase):
    def test_real_child_reaches_only_owned_loopback_server(self):
        result = check_environment()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['owned_local_requests'], 1)

    def test_child_claiming_success_without_a_request_is_rejected(self):
        with patch('tools.validation.validation_environment.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, b'', b'')):
            with self.assertRaisesRegex(RuntimeError, 'owned localhost'):
                check_environment()
