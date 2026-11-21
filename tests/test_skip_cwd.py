import contextlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class SkipCwdTests(unittest.TestCase):
    def test_skip_cwd_from_home_and_explicit_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[default]\nimage = "test"\n')
            for option in ("-s", "--skip-cwd"):
                for paths in ([], [directory]):
                    with self.subTest(option=option, paths=paths), \
                         patch.object(Path, "cwd", return_value=Path.home()), \
                         patch.object(sb, "image_cached", return_value=True), \
                         patch.dict(os.environ), contextlib.redirect_stdout(io.StringIO()) as output:
                        sb.main(["--config", str(config), "--debug", option, *paths])
                        command = shlex.split(output.getvalue())
                        expected = os.path.realpath(directory) if paths else str(Path.home())
                        self.assertEqual(command[command.index("-w") + 1], expected)
                        if paths:
                            self.assertIn(f"{expected}:{expected}", command)
                        else:
                            self.assertNotIn("-v", command)

    def test_home_without_skip_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[default]\nimage = "test"\n')
            with patch.object(Path, "cwd", return_value=Path.home()), \
                 self.assertRaisesRegex(SystemExit, "--skip-cwd"):
                sb.main(["--config", str(config)])


if __name__ == "__main__":
    unittest.main()
