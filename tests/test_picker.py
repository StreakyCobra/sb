import unittest

import sb

try:
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
except ModuleNotFoundError:
    create_pipe_input = None


@unittest.skipIf(create_pipe_input is None, "Prompt Toolkit is not installed")
class PickerTests(unittest.TestCase):
    def pick(self, keys, preselected=()):
        with create_pipe_input() as terminal, create_app_session(input=terminal, output=DummyOutput()):
            terminal.send_text(keys)
            terminal.close()
            return sb.pick_groups(["java", "python", "circleci", "python-3"], preselected, True)

    def test_enter_keeps_flag_order(self):
        self.assertEqual(self.pick("\r", ["python", "java", "python"]), ["python", "java"])

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
