"""External programs must keep their system library search paths in frozen Linux builds."""
import os
import unittest
from unittest.mock import patch

from rockbox_manager import frozen_runtime as runtime


class FrozenRuntimeTests(unittest.TestCase):
    def test_source_runs_leave_subprocess_environment_inheritance_unchanged(self):
        with patch.object(runtime.sys, "frozen", False, create=True), \
                patch.object(runtime.sys, "platform", "linux"):
            self.assertIsNone(runtime.external_environment())

    def test_frozen_linux_restores_user_library_path_without_changing_parent(self):
        values = {"LD_LIBRARY_PATH": "/bundle:/user/libs", "LD_LIBRARY_PATH_ORIG": "/user/libs", "PATH": "/bin"}
        with patch.dict(os.environ, values, clear=True), patch.object(runtime.sys, "frozen", True, create=True), \
                patch.object(runtime.sys, "platform", "linux"):
            env = runtime.external_environment()
            self.assertEqual(env["LD_LIBRARY_PATH"], "/user/libs")
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/bundle:/user/libs")

    def test_frozen_linux_without_original_path_uses_system_libraries(self):
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle"}, clear=True), \
                patch.object(runtime.sys, "frozen", True, create=True), patch.object(runtime.sys, "platform", "linux"):
            self.assertNotIn("LD_LIBRARY_PATH", runtime.external_environment())
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/bundle")
