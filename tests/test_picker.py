import contextlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb

try:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
except ModuleNotFoundError:
    create_pipe_input = None


class PickerEntryTests(unittest.TestCase):
    def test_default_includes_are_applied_without_appearing_in_the_picker(self):
        defaults = """\
[default]
image = "test"
include = ["base"]
[base]
include = ["docker"]
[docker]
env = { DOCKER = "on" }
"""
        optional = '[java]\ninclude = ["maven"]\n[maven]\n'
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            for extra, flags in ((optional, ["--java"]), ("", [])):
                with self.subTest(flags=flags):
                    config.write_text(defaults + extra)
                    output = io.StringIO()
                    selection = ("test-project", ["java"] if extra else [])
                    with (patch.dict(os.environ),
                          contextlib.redirect_stdout(output),
                          patch.object(output, "isatty", return_value=True),
                          patch.object(sb.sys.stdin, "isatty", return_value=True),
                          patch.object(sb, "image_cached", return_value=True),
                          patch.object(sb, "load_group_usage", return_value={}),
                          patch.object(sb, "pick_startup", return_value=selection) as picker):
                        sb.main(["--config", str(config), "--debug", "-s", "--base", *flags])
                    self.assertIn("DOCKER=on", shlex.split(output.getvalue()))
                    picker.assert_called_once()
                    project, names, _, groups, usage = picker.call_args.args
                    self.assertEqual(project, "workspace")
                    self.assertEqual(names, ["java", "maven"] if extra else [])
                    self.assertEqual(groups, sb.load_groups(config))
                    self.assertEqual(usage, {})


@unittest.skipIf(create_pipe_input is None, "Prompt Toolkit is not installed")
class PickerTests(unittest.TestCase):
    def pick(self, keys, preselected=(), usage=None, *, groups=None, rendered=None):
        if groups is None:
            groups = {name: {} for name in ("java", "python", "circleci", "python-3")}
        result = self.startup("\r" + keys, lambda _: preselected, groups, usage, rendered)
        return result[1] if result is not None else None

    def startup(self, keys, preselect, groups, usage=None, rendered=None):
        output = DummyOutput()
        if rendered is not None:
            output.write = rendered.write
        with create_pipe_input() as terminal, create_app_session(input=terminal, output=output):
            terminal.send_text(keys)
            terminal.close()
            return sb.pick_startup("current-project", list(groups), preselect, groups, usage or {})

    def test_typing_replaces_prefilled_project_and_loads_its_groups(self):
        groups = {"java": {}, "python": {}}
        saved = {"current-project": ["java"], "my job": ["python"]}
        rendered = io.StringIO()
        self.assertEqual(self.startup("my job\r\r", saved.get, groups, rendered=rendered),
                         ("my job", ["python"]))
        self.assertIn("current-project", rendered.getvalue())

    def test_tab_back_keeps_group_changes_if_project_is_unchanged(self):
        self.assertEqual(self.startup("\t \t\t\r", lambda _: [], {"java": {}}),
                         ("current-project", ["java"]))

    def test_paste_replaces_project_when_there_are_no_optional_groups(self):
        self.assertEqual(self.startup("\x1b[200~new-project\x1b[201~\r", None, {}),
                         ("new-project", []))

    def test_preselected_group_shows_all_includes_checked(self):
        groups = {"web": {"include": ["npm"]}, "npm": {"include": ["node"]}, "node": {}}
        rendered = io.StringIO()
        self.assertEqual(self.pick("\r", ["web"], groups=groups, rendered=rendered), ["web"])
        self.assertIn("[x] npm (included)", rendered.getvalue())
        self.assertIn("[x] node (included)", rendered.getvalue())

    def test_included_group_can_stay_selected_on_its_own(self):
        groups = {"web": {"include": ["npm"]}, "npm": {}}
        # Select web, explicitly select npm, then deselect web.
        self.assertEqual(self.pick(" j k \r", groups=groups), ["npm"])

    def test_usage_orders_full_and_filtered_lists(self):
        usage = {"circleci": 8, "python-3": 3, "python": 1}
        self.assertEqual(self.pick(" \r", usage=usage), ["circleci"])
        self.assertEqual(self.pick("py \r", usage=usage), ["python-3"])

    def test_enter_keeps_flag_order(self):
        self.assertEqual(self.pick("\r", ["python", "java", "python"], {"java": 5}), ["python", "java"])

    def test_arrows_and_vim_keys_move_between_checkboxes(self):
        self.assertEqual(self.pick("\x1b[B j k \r"), ["circleci"])

    def test_fuzzy_search_resets_without_losing_checked_groups(self):
        # Select python-3 and search for circleci after clearing a two-letter query.
        # Enter still confirms checked groups when the last search has no matches.
        self.assertEqual(self.pick("ptn3 zq\x7fci z\r", ["java"]), ["java", "python-3", "circleci"])

    def test_space_clears_search_even_without_matches(self):
        self.assertEqual(self.pick("z\x1b[B j \r"), ["python"])

    def test_ctrl_c_cancels(self):
        self.assertIsNone(self.pick("\x03", ["java"]))


if __name__ == "__main__":
    unittest.main()
