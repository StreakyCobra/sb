import contextlib
import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sb


class AllowlistTests(unittest.TestCase):
    def test_requested_syntax(self):
        cases = {
            "allow@chatgpt.com:tcp:443": "allow@chatgpt.com:tcp:443",
            "google.com:tcp:443": "allow@google.com:tcp:443",
            "facebook.com:443": "allow@facebook.com:tcp:443",
            "instagram.com": "allow@instagram.com",
        }
        for entry, expected in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(sb.allowlist_rule(entry), expected)

    def test_additional_msb_syntax(self):
        cases = {
            "  example.com:443  ": "allow@example.com:tcp:443",
            "allow@example.com:443": "allow@example.com:tcp:443",
            "example.com:8000-9000": "allow@example.com:tcp:8000-9000",
            "example.com:udp:123": "allow@example.com:udp:123",
            "*.example.com:443": "allow@*.example.com:tcp:443",
            "192.0.2.1:443": "allow@192.0.2.1:tcp:443",
            "[2001:db8::1]:443": "allow@[2001:db8::1]:tcp:443",
            "[2001:db8::1]:tcp:443": "allow@[2001:db8::1]:tcp:443",
            "[2001:db8::1]": "allow@[2001:db8::1]",
            "dns": "allow@dns",
        }
        for entry, expected in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(sb.allowlist_rule(entry), expected)

    def test_invalid_entries(self):
        for entry in ("", " ", "allow@", "deny@example.com", "allow:ingress@example.com",
                      "example.com,allow@any", "example.com other.com", ":443",
                      "[2001:db8::1", "[2001:db8::1]443", None, 443):
            with self.subTest(entry=entry), self.assertRaises(SystemExit):
                sb.allowlist_rule(entry)

    def load_config(self, text):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(text)
            return sb.load_groups(config)

    def test_configuration_rejects_invalid_allowlist(self):
        for value in ('"example.com"', "false", "[443]", '["deny@example.com"]'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.load_config(f"[default]\nallowlist = {value}\n")

    def test_lists_merge_from_default_includes_and_selected_groups(self):
        groups = self.load_config("""
[default]
allowlist = ["example.com:443"]
[shared]
allowlist = ["mcp.kagi.com:443"]
[agent]
include = ["shared"]
allowlist = ["chatgpt.com:443"]
[unselected]
allowlist = ["other.com"]
""")
        settings = sb.merge(groups, sb.resolve(groups, ["agent", "shared"]))
        self.assertEqual(settings["allowlist"], [
            "example.com:443", "mcp.kagi.com:443", "chatgpt.com:443",
        ])
        self.assertEqual(sb.allowlist_flags(settings["allowlist"]), [
            "--net-default-egress", "deny",
            "--net-rule", "allow@dns",
            "--net-rule", "allow@example.com:tcp:443",
            "--net-rule", "allow@mcp.kagi.com:tcp:443",
            "--net-rule", "allow@chatgpt.com:tcp:443",
        ])

    def debug_command(self, text, *args):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text(text)
            output = io.StringIO()
            with patch.object(sb, "image_cached", return_value=True), \
                    patch.dict(os.environ), contextlib.redirect_stdout(output):
                sb.main(["--config", str(config), "--debug", directory, *args])
            return shlex.split(output.getvalue())

    def test_empty_allowlist_enables_default_deny_and_keeps_host_ports(self):
        command = self.debug_command("""
[default]
image = "test"
allowlist = []
host_ports = [1869]
""")
        start = command.index("--net-default-egress")
        self.assertEqual(command[start:], [
            "--net-default-egress", "deny",
            "--net-rule", "allow@dns",
            "--net-rule", "allow@host:tcp:1869",
            "test",
        ])
        self.assertNotIn("--net-default-ingress", command)
        self.assertNotIn("--net-default", command)

    def test_omitted_and_unselected_allowlists_preserve_existing_behavior(self):
        command = self.debug_command("""
[default]
image = "test"
[restricted]
allowlist = []
""")
        self.assertNotIn("--net-default-egress", command)
        self.assertNotIn("--net-rule", command)

    def test_selected_group_enables_allowlist(self):
        command = self.debug_command("""
[default]
image = "test"
[restricted]
allowlist = ["facebook.com:443"]
""", "--restricted")
        self.assertIn("--net-default-egress", command)
        self.assertIn("allow@facebook.com:tcp:443", command)


if __name__ == "__main__":
    unittest.main()
