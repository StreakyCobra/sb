import contextlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class CommandTests(unittest.TestCase):
    def test_shell_substitution_is_passed_to_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text("""\
[default]
image = "test"
command = ["sh", "-c", 'exec codex -c "projects.\\"$(git rev-parse --show-toplevel)\\".trust_level=trusted"']
""")
            output = io.StringIO()
            with (patch.dict(os.environ),
                  patch.object(sb, "image_cached", return_value=True),
                  contextlib.redirect_stdout(output)):
                sb.main(["--debug", "--config", str(config), directory])
            command = shlex.split(output.getvalue())
            self.assertEqual(command[command.index("--") + 1:], [
                "sh", "-c", 'exec codex -c "projects.\\"$(git rev-parse --show-toplevel)\\".trust_level=trusted"',
            ])

    def test_unset_variables_still_fail(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "unset variable"):
                sb.expand("$$MISSING")
            with self.assertRaisesRegex(SystemExit, "unset variable"):
                sb.expand("$${MISSING}")

    def test_escape_and_host_variable_expansion(self):
        with patch.dict(os.environ, {"HOST_VALUE": "host"}, clear=True):
            self.assertEqual(
                sb.expand("$$HOST_VALUE $${HOST_VALUE} ${CONTAINER_VALUE} $HOME $(command)"),
                "host host ${CONTAINER_VALUE} $HOME $(command)",
            )
            self.assertEqual(sb.expand("$MISSING"), "$MISSING")


if __name__ == "__main__":
    unittest.main()
