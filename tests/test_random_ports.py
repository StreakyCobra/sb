import contextlib
import io
import os
import shlex
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class RandomPortTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = self.directory / "config.toml"

    def load_config(self, text):
        self.config.write_text(text)
        return sb.load_groups(self.config)

    def run_main(self, text, *args, debug=True):
        self.config.write_text(text)
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--config", str(self.config), str(self.directory), *args]
        if debug:
            argv.insert(0, "--debug")
        with (
            patch.dict(os.environ),
            patch.object(sb, "image_cached", return_value=True),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            sb.main(argv)
        return shlex.split(stdout.getvalue(), comments=True), stderr.getvalue()

    def flag_values(self, command, flag):
        return [command[index + 1] for index, value in enumerate(command) if value == flag]

    def test_configuration_accepts_variable_names_and_empty_lists(self):
        groups = self.load_config("""
[default]
random_ports = []
[review]
random_ports = ["SB_DIFIT_PORT", "_port2"]
""")
        self.assertEqual(groups["default"]["random_ports"], [])
        self.assertEqual(groups["review"]["random_ports"], ["SB_DIFIT_PORT", "_port2"])

    def test_configuration_rejects_invalid_names_and_types(self):
        for value in ('"SB_PORT"', "false", "[443]", '[""]', '["1PORT"]', '["SB-PORT"]',
                      '["SB PORT"]', '["PORT=123"]', '["PÖRT"]', '["SB_PORT\\n"]'):
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "random.port"):
                self.load_config(f"[default]\nrandom_ports = {value}\n")

    def test_choices_are_distinct_deduplicated_and_do_not_open_sockets(self):
        with patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")):
            values = sb.choose_random_ports(["SB_FIRST", "SB_SECOND", "SB_FIRST"], [])
        self.assertEqual(list(values), ["SB_FIRST", "SB_SECOND"])
        self.assertEqual(len(set(values.values())), 2)
        for port in values.values():
            self.assertTrue(49152 <= int(port) <= 65535)

    def test_fixed_tcp_ports_include_ranges_and_ipv6_but_not_udp(self):
        self.assertEqual(sb.fixed_tcp_ports([
            "3000:3000",
            "127.0.0.1:4000-4002:80-82/tcp",
            "[::]:5000:80",
            "127.0.0.1:6000:80/udp",
            "invalid",
            "invalid:80",
            "99999:80",
        ]), {3000, 4000, 4001, 4002, 5000})

    def test_choices_skip_fixed_tcp_ports(self):
        def sample(candidates, count):
            self.assertNotIn(49152, candidates)
            self.assertNotIn(49153, candidates)
            self.assertIn(49154, candidates)
            self.assertEqual(count, 1)
            return [49154]

        with patch.object(sb.random, "sample", side_effect=sample):
            values = sb.choose_random_ports(["SB_PORT"], ["127.0.0.1:49152-49153:80-81"])
        self.assertEqual(values, {"SB_PORT": "49154"})

    def test_all_candidates_excluded_reports_an_error_without_sampling(self):
        with patch.object(sb.random, "sample") as sample:
            with self.assertRaisesRegex(SystemExit, "not enough random TCP ports"):
                sb.choose_random_ports(["SB_PORT"], ["49152-65535:49152-65535"])
        sample.assert_not_called()

    def test_selected_groups_merge_and_duplicate_names_get_one_mapping(self):
        command, stderr = self.run_main("""
[default]
image = "test"
random_ports = ["SB_FIRST"]
[shared]
random_ports = ["SB_SECOND", "SB_FIRST"]
[agent]
include = ["shared"]
random_ports = ["SB_SECOND"]
[unselected]
random_ports = ["SB_UNUSED"]
""", "--agent", "--shared")
        environment = dict(value.split("=", 1) for value in self.flag_values(command, "-e"))
        mappings = self.flag_values(command, "-p")
        self.assertEqual(set(environment), {"SB_FIRST", "SB_SECOND"})
        self.assertEqual(len(set(environment.values())), 2)
        self.assertEqual(mappings, [
            f"127.0.0.1:{environment['SB_FIRST']}:{environment['SB_FIRST']}",
            f"127.0.0.1:{environment['SB_SECOND']}:{environment['SB_SECOND']}",
        ])
        self.assertEqual(stderr, "")

    def test_generated_value_replaces_host_value_and_supports_expansion(self):
        with patch.dict(os.environ, {"SB_DIFIT_PORT": "1"}):
            command, _ = self.run_main("""
[default]
image = "test"
random_ports = ["SB_DIFIT_PORT"]
env = { VIEWER_URL = "http://127.0.0.1:$${SB_DIFIT_PORT}" }
command = ["viewer", "$${SB_DIFIT_PORT}"]
""")
        environment = dict(value.split("=", 1) for value in self.flag_values(command, "-e"))
        port = environment["SB_DIFIT_PORT"]
        self.assertNotEqual(port, "1")
        self.assertEqual(environment["VIEWER_URL"], f"http://127.0.0.1:{port}")
        self.assertEqual(command[-3:], ["--", "viewer", port])

    def test_fixed_mappings_and_command_override_are_preserved(self):
        command, _ = self.run_main("""
[default]
image = "test"
random_ports = ["SB_DIFIT_PORT"]
ports = ["127.0.0.1:3000:3000"]
command = ["agent"]
""", "-p", "127.0.0.1:3001:3001", "--", "zsh")
        self.assertEqual(self.flag_values(command, "-p")[:2], [
            "127.0.0.1:3000:3000", "127.0.0.1:3001:3001",
        ])
        self.assertEqual(len(self.flag_values(command, "-p")), 3)
        self.assertEqual(command[-2:], ["--", "zsh"])

    def test_variable_conflicts_are_rejected_before_allocation(self):
        cases = [
            ('env = { SB_PORT = "123" }', (), "SB_PORT"),
            ('secrets = { SB_PORT = { hosts = ["example.com"] } }', (), "SB_PORT"),
            ("", ("-e", "SB_PORT=123"), "SB_PORT"),
            ("", (), "SB_PROJECT"),
        ]
        for extra, args, variable in cases:
            with self.subTest(extra=extra, args=args, variable=variable):
                with patch.object(sb, "choose_random_ports") as allocate:
                    with self.assertRaisesRegex(SystemExit, "already defined"):
                        self.run_main(f"""
[default]
image = "test"
random_ports = ["{variable}"]
{extra}
""", *args)
                    allocate.assert_not_called()

    def test_unselected_group_does_not_allocate_ports(self):
        with patch.object(sb.random, "sample", side_effect=AssertionError("unexpected allocation")):
            command, stderr = self.run_main("""
[default]
image = "test"
[review]
random_ports = ["SB_DIFIT_PORT"]
""")
        self.assertNotIn("-p", command)
        self.assertNotIn("-e", command)
        self.assertEqual(stderr, "")

    def test_debug_does_not_execute_msb_or_read_secrets(self):
        with patch.object(sb.os, "execvpe") as execute, patch.object(sb, "secret_value") as secret:
            command, stderr = self.run_main("""
[default]
image = "test"
random_ports = ["SB_DIFIT_PORT"]
secrets = { TOKEN = { cmd = "pass show token", hosts = ["example.com"] } }
""")
        execute.assert_not_called()
        secret.assert_not_called()
        self.assertEqual(command[:4], ["export", "TOKEN=$(pass show token | head -n 1)", "msb", "run"])
        self.assertIn("-p", command)
        self.assertEqual(stderr, "")

    def test_launch_does_not_open_sockets_and_environment_matches_mapping(self):
        def execute(binary, command, environment):
            self.assertEqual(binary, "msb")
            port = environment["SB_DIFIT_PORT"]
            self.assertIn(f"SB_DIFIT_PORT={port}", self.flag_values(command, "-e"))
            self.assertIn(f"127.0.0.1:{port}:{port}", self.flag_values(command, "-p"))

        with (
            patch.object(sb.os, "execvpe", side_effect=execute) as launch,
            patch.object(socket, "socket", side_effect=AssertionError("unexpected socket")),
        ):
            command, stderr = self.run_main("""
[default]
image = "test"
random_ports = ["SB_DIFIT_PORT"]
command = ["zsh"]
""", debug=False)
        launch.assert_called_once()
        self.assertEqual(command, [])
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
