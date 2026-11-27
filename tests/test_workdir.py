import contextlib
import hashlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class WorkdirTests(unittest.TestCase):
    def test_workdir_from_mount_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / "config.toml"
            config.write_text('[default]\nimage = "test"\n')
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            file = first / "file"
            file.touch()
            for paths, workdir in (
                ([first, second], root),
                ([second, first], root),
                ([file, second], root),
                ([first], first),
                ([file], first),
                ([first, Path("/dev")], Path.home()),
            ):
                with (self.subTest(paths=paths),
                      patch.object(sb, "image_cached", return_value=True),
                      patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()) as output):
                    sb.main(["--config", str(config), "--debug", *map(str, paths)])
                    command = shlex.split(output.getvalue())
                    self.assertEqual(command[command.index("-w") + 1], str(workdir))
                    project = hashlib.sha256(b"\0".join(sorted(map(os.fsencode, paths)))).hexdigest()[:16]
                    self.assertEqual(os.environ["SB_PROJECT"], project)
                    self.assertEqual([command[i + 1] for i, arg in enumerate(command)
                                      if arg in ("-v", "--mount-file")],
                                     [f"{path}:{path}" for path in paths])

    def test_current_folder_uses_its_name_with_or_without_explicit_dot(self):
        cwd = Path.cwd().resolve()
        for paths in ([], ["."], [".", str(cwd)]):
            with self.subTest(paths=paths):
                self.assertEqual(sb.project_mounts(paths, False), ([str(cwd)], cwd.name))
