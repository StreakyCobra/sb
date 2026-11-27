import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class MountTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.store = self.home / ".local/share/sb/opencode"
        self.config = self.home / "config.toml"
        self.config.write_text("""\
[default]
image = "test"
[opencode]
auto_create_mount_dirs = ["~/.local/share/sb/opencode/"]
mounts = ["~/.local/share/sb/opencode/$${SB_PROJECT}:/agent:rw"]
""")
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(patch.object(sb.sys.stdin, "isatty", return_value=False))
        self.enterContext(patch.object(sb, "image_cached", return_value=True))
        self.enterContext(patch.object(sb.shutil, "which", return_value=None))
        self.execute = self.enterContext(patch.object(sb.os, "execvpe"))
        self.argv = ["--config", str(self.config), "--no-picker", "-s"]

    def test_selected_group_creates_project_mount_without_confirmation(self):
        sb.main([*self.argv, "--opencode"])
        project = self.store / "workspace"
        self.assertTrue(project.is_dir())
        self.assertIn(f"{project}:/agent:rw", self.execute.call_args.args[1])

    def test_unselected_group_does_not_allow_automatic_creation(self):
        with self.assertRaisesRegex(SystemExit, "mount source does not exist"):
            sb.main([*self.argv, "-v", str(self.store)])
        self.assertFalse(self.store.exists())

    def test_debug_does_not_create_allowlisted_mount(self):
        with (contextlib.redirect_stderr(io.StringIO()) as warnings,
              contextlib.redirect_stdout(io.StringIO())):
            sb.main([*self.argv, "--opencode", "--debug"])
        self.assertIn(f"mount source does not exist: {self.store / 'workspace'}", warnings.getvalue())
        self.assertFalse(self.store.exists())

    def test_lookalike_paths_and_paths_resolving_outside_still_prompt(self):
        self.store.mkdir(parents=True)
        external = self.home / "external"
        external.mkdir()
        (self.store / "link").symlink_to(external, target_is_directory=True)
        for source in (self.store.with_name("opencode-other") / "project",
                       self.store / ".." / "project",
                       self.store / "link" / "project"):
            with (self.subTest(source=source),
                  patch.object(sb.sys.stdin, "isatty", return_value=True),
                  patch("builtins.input", return_value="n") as prompt,
                  self.assertRaisesRegex(SystemExit, "mount source does not exist")):
                sb.main([*self.argv, "--opencode", "-v", str(source)])
            prompt.assert_called_once()
            self.assertFalse(source.exists())

    def test_automatic_creation_uses_the_resolved_path(self):
        self.store.mkdir(parents=True)
        outside = self.store.with_name("unused")
        source = outside / ".." / self.store.name / "project"
        target = self.store / "project"
        sb.main([*self.argv, "--opencode", "-v", f"{source}:/project"])
        self.assertFalse(outside.exists())
        self.assertTrue(target.is_dir())
        self.assertIn(f"{target}:/project", self.execute.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
