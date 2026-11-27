import contextlib
import io
import os
import sqlite3
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

    def saved_groups(self, project="workspace", config=None):
        return sb.load_project_groups(self.state, (config or self.config).as_uri(), project)

    def open_picker(self, simulate_picker, *argv):
        with (patch.object(sb.sys.stdin, "isatty", return_value=True),
              patch.object(sb.sys.stdout, "isatty", return_value=True),
              patch.object(sb, "pick_startup", side_effect=simulate_picker)):
            sb.main(["--config", str(self.config), *argv])

    def test_state_defaults_to_local_state(self):
        for value in ("", "relative"):
            with self.subTest(value=value), patch.dict(os.environ, {"XDG_STATE_HOME": value}):
                self.assertEqual(
                    sb.state_database_path(),
                    Path.home() / ".local/state/sb/state.sqlite3",
                )

    def test_picker_uses_counts_and_selections_from_existing_database(self):
        self.state.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.state)) as db, db:
            db.execute("""
                CREATE TABLE group_usage (config TEXT NOT NULL, name TEXT NOT NULL,
                                          launches INTEGER NOT NULL, PRIMARY KEY (config, name))
            """)
            db.execute("INSERT INTO group_usage VALUES (?, 'python', 1)", (self.config.as_uri(),))
            db.execute("""
                CREATE TABLE project_launches (id INTEGER PRIMARY KEY, config TEXT NOT NULL,
                                               project TEXT NOT NULL, groups TEXT NOT NULL)
            """)
            db.execute("INSERT INTO project_launches VALUES (1, ?, 'workspace', ?)",
                       (self.config.as_uri(), '["java"]'))

        def pick(project, names, preselect, groups, usage):
            self.assertEqual(usage, {"python": 1})
            self.assertEqual(preselect(project), ["java"])
            return project, ["python"]

        self.open_picker(pick, "-s")
        self.assertEqual(self.usage(), {"python": 2})
        self.assertEqual(self.saved_groups(), ["python"])

    def test_launch_counts_only_explicit_groups_and_keeps_configurations_separate(self):
        sb.main(["--config", str(self.config), "-s", "--no-picker", "--python", "--python"])
        sb.main(["--config", str(self.config), "-s", "--no-picker", "--java", "--python"])
        other = self.config.with_name("other.toml")
        other.write_text(self.config.read_text())
        sb.main(["--config", str(other), "-s", "--no-picker", "--java"])
        self.assertEqual(self.usage(), {"python": 2, "java": 1})
        self.assertEqual(self.usage(other), {"java": 1})
        self.assertEqual(self.saved_groups(), ["java", "python"])
        self.assertEqual(self.saved_groups(config=other), ["java"])

    def test_picker_reads_and_saves_groups_for_the_custom_name(self):
        self.config.write_text('[default]\nimage = "test"\nenv = { DB = "$${SB_PROJECT}.db" }\n[java]\n[python]\n')

        def rename(project, names, preselect, groups, usage):
            return "my-project", ["python"]

        self.open_picker(rename, "-s")
        self.assertEqual(self.saved_groups("my-project"), ["python"])
        self.assertEqual(self.saved_groups(), [])
        _, command, env = self.execute.call_args.args
        self.assertIn("DB=my-project.db", command)
        self.assertEqual(env["SB_PROJECT"], "my-project")

        def reopen(project, names, preselect, groups, usage):
            self.assertEqual(preselect(project), ["java"])
            self.assertEqual(preselect("my-project"), ["python", "java"])
            return "my-project", ["python", "java"]

        self.open_picker(reopen, "-s", "--java")
        self.assertEqual(self.saved_groups("my-project"), ["python", "java"])

    def test_picker_remembers_the_name_for_the_same_paths_in_any_order(self):
        first, second = (self.config.parent / name for name in ("folderA", "folderB"))
        first.mkdir()
        second.mkdir()

        def rename(project, names, preselect, groups, usage):
            self.assertRegex(project, r"^[a-f0-9]{16}$")
            return "paired", ["python"]

        self.open_picker(rename, str(first), str(second))

        def reopen(project, names, preselect, groups, usage):
            self.assertEqual(project, "paired")
            self.assertEqual(preselect(project), ["python"])
            return project, preselect(project)

        self.open_picker(reopen, str(second), str(first))
        argv = ["--config", str(self.config), "--no-picker"]
        sb.main([*argv, str(second), str(first)])
        self.assertEqual(self.execute.call_args.args[2]["SB_PROJECT"], "paired")
        sb.main([*argv, str(first)])
        self.assertRegex(self.execute.call_args.args[2]["SB_PROJECT"], r"^[a-f0-9]{16}$")

    def test_current_folder_inherits_until_it_has_its_own_selection(self):
        root = self.config.parent
        base = root / "swan-lake-infra"
        child = root / "swan-lake-infra--implement-probes--next"
        base.mkdir()
        child.mkdir()
        argv = ["--config", str(self.config), "--no-picker"]
        with patch.object(Path, "cwd", return_value=base):
            sb.main([*argv, "--python", "--java"])

        def first_open(project, names, preselect, groups, usage):
            self.assertEqual(project, child.name)
            self.assertEqual(preselect(project), ["python", "java"])
            return project, []

        def second_open(project, names, preselect, groups, usage):
            self.assertEqual(preselect(project), [])
            return project, []

        with patch.object(Path, "cwd", return_value=child):
            self.open_picker(first_open)
            self.open_picker(second_open)
        self.assertEqual(self.saved_groups(base.name), ["python", "java"])

        def open_explicit_paths(project, names, preselect, groups, usage):
            self.assertEqual(preselect(f"{base.name}--other"), [])
            return project, []

        self.open_picker(open_explicit_paths, str(root))

    def test_debug_and_cancel_do_not_create_usage(self):
        argv = ["--config", str(self.config), "-s", "--java"]
        with contextlib.redirect_stdout(io.StringIO()):
            sb.main([*argv, "--no-picker", "--debug"])
        with (patch.object(sb.sys.stdin, "isatty", return_value=True),
              patch.object(sb.sys.stdout, "isatty", return_value=True),
              patch.object(sb, "pick_startup", return_value=None)):
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
        self.assertEqual(self.saved_groups(), ["python"])

    def test_failed_exec_does_not_replace_another_launch(self):
        config = self.config.as_uri()
        mount_hash = "same-mounts"
        earlier = sb.update_launch_state(self.state, config, "workspace", ["java"], mount_hash=mount_hash)
        later = sb.update_launch_state(self.state, config, "workspace", ["python"], mount_hash=mount_hash)
        sb.update_launch_state(self.state, config, "workspace", ["java"], undo=earlier)
        self.assertEqual(self.saved_groups(), ["python"])
        self.assertEqual(sb.load_project_name(self.state, config, mount_hash), "workspace")
        sb.update_launch_state(self.state, config, "workspace", ["python"], undo=later)
        self.assertEqual(self.saved_groups(), [])
        self.assertIsNone(sb.load_project_name(self.state, config, mount_hash))

    def test_unusable_state_does_not_prevent_launch(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("not a database")
        sb.main(["--config", str(self.config), "-s", "--no-picker", "--java"])
        self.execute.assert_called_once()
        self.assertEqual(self.usage(), {})


if __name__ == "__main__":
    unittest.main()
