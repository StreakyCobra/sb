import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class GroupUsageTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = directory / "config.toml"
        self.config.write_text('[default]\nimage = "test"\n[shared]\n[java]\n[python]\ninclude = ["shared"]\n')
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(directory / "state")}))
        self.enterContext(patch.object(sb, "image_cached", return_value=True))
        self.which = self.enterContext(patch.object(sb.shutil, "which", return_value="/path/to/msb"))
        self.execute = self.enterContext(patch.object(sb.os, "execvpe"))
        self.state = directory / "state" / "sb" / "state.sqlite3"

    def usage(self, config=None):
        return sb.load_group_usage(self.state, (config or self.config).as_uri())

    def test_state_defaults_to_local_state(self):
        for value in ("", "relative"):
            with self.subTest(value=value), patch.dict(os.environ, {"XDG_STATE_HOME": value}):
                self.assertEqual(
                    sb.state_database_path(),
                    Path.home() / ".local/state/sb/state.sqlite3",
                )

    def test_picker_uses_saved_counts(self):
        config = self.config.as_uri()
        self.assertTrue(sb.update_group_usage(self.state, config, ["python"]))
        argv = ["--config", str(self.config), "-s"]
        with (patch.object(sb.sys.stdin, "isatty", return_value=True),
              patch.object(sb.sys.stdout, "isatty", return_value=True),
              patch.object(sb, "pick_groups", return_value=["python"]) as picker):
            sb.main(argv)
        self.assertEqual(picker.call_args.args[-1], {"python": 1})
        self.assertEqual(self.usage(), {"python": 2})

    def test_launch_counts_only_explicit_groups_and_keeps_configurations_separate(self):
        sb.main(["--config", str(self.config), "-s", "--no-picker", "--python", "--python"])
        sb.main(["--config", str(self.config), "-s", "--no-picker", "--java", "--python"])
        other = self.config.with_name("other.toml")
        other.write_text(self.config.read_text())
        sb.main(["--config", str(other), "-s", "--no-picker", "--java"])
        self.assertEqual(self.usage(), {"python": 2, "java": 1})
        self.assertEqual(self.usage(other), {"java": 1})

    def test_debug_and_cancel_do_not_create_usage(self):
        argv = ["--config", str(self.config), "-s", "--java"]
        with contextlib.redirect_stdout(io.StringIO()):
            sb.main([*argv, "--no-picker", "--debug"])
        with (patch.object(sb.sys.stdin, "isatty", return_value=True),
              patch.object(sb.sys.stdout, "isatty", return_value=True),
              patch.object(sb, "pick_groups", return_value=None)):
            with self.assertRaises(SystemExit) as cancelled:
                sb.main(argv)
            self.assertEqual(cancelled.exception.code, 130)
        self.execute.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_missing_msb_does_not_create_usage(self):
        self.which.return_value = None
        self.execute.side_effect = FileNotFoundError
        with self.assertRaisesRegex(SystemExit, "msb not found"):
            sb.main(["--config", str(self.config), "-s", "--no-picker", "--python"])
        self.assertFalse(self.state.exists())

    def test_failed_exec_leaves_previous_usage_intact(self):
        argv = ["--config", str(self.config), "-s", "--no-picker", "--python"]
        sb.main(argv)
        self.execute.side_effect = PermissionError
        with self.assertRaises(PermissionError):
            sb.main(argv)
        self.assertEqual(self.usage(), {"python": 1})

    def test_unusable_state_does_not_prevent_launch(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("not a database")
        sb.main(["--config", str(self.config), "-s", "--no-picker", "--java"])
        self.execute.assert_called_once()
        self.assertEqual(self.usage(), {})


if __name__ == "__main__":
    unittest.main()
