#!/usr/bin/env python3
"""sb: compose an `msb run` command from groups defined in a TOML file.

    sb [--config FILE] [--debug] [--no-picker] [-s] [--no-network-filter]
       [-p PORT] [-v MOUNT] [-e KEY=VALUE]
       [--GROUP ...] [PATH ...] [-- COMMAND ...]

Each PATH (default: the current directory) is mounted at the same absolute path
in the sandbox. The group named "default" is always applied. In a terminal,
use the picker to name the project and select other groups; --GROUP preselects
a group.
-p, -v and -e add to the ports, mounts and env of the selected groups.
See example.toml for the file format.
"""

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from contextlib import closing
from functools import partial
from pathlib import Path

DEFAULT_GROUP = "default"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
DEFAULT_CONFIG = CONFIG_HOME / "sb" / "config.toml"

SECRET_KEYS = {"cmd", "hosts", "headers"}
BUILTIN_OPTIONS = {
    "config", "debug", "help", "port", "mount", "env", "skip-cwd", "no-network-filter", "no-picker",
}


def fail(message):
    sys.exit(f"sb: {message}")


def state_database_path():
    home = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not home.is_absolute():
        home = Path.home() / ".local/state"
    return home / "sb" / "state.sqlite3"


def load_group_usage(path, config):
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as db:
            return dict(db.execute("SELECT name, launches FROM group_usage WHERE config = ?", (config,)))
    except sqlite3.Error:
        return {}


def load_project_groups(path, config, project, *, inherit=False):
    base, separator, _ = project.partition("--")
    candidates = (project, base) if inherit and separator and base else (project,)
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.5)) as db:
            for name in candidates:
                row = db.execute("""
                    SELECT groups FROM project_launches WHERE config = ? AND project = ?
                    ORDER BY id DESC LIMIT 1
                """, (config, name)).fetchone()
                if row is not None:
                    return json.loads(row[0])
    except (sqlite3.Error, ValueError):
        pass
    return []


def update_launch_state(path, config, project, names, *, undo=None):
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            # Wait for other writers before creating the table or updating counts.
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS group_usage (
                    config TEXT NOT NULL,
                    name TEXT NOT NULL,
                    launches INTEGER NOT NULL,
                    PRIMARY KEY (config, name)
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS project_launches (
                    id INTEGER PRIMARY KEY,
                    config TEXT NOT NULL,
                    project TEXT NOT NULL,
                    groups TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS project_launches_by_project
                ON project_launches (config, project, id)
            """)
            if undo is not None:
                db.execute("DELETE FROM project_launches WHERE id = ?", (undo,))
                db.executemany("""
                    UPDATE group_usage SET launches = launches - 1
                    WHERE config = ? AND name = ? AND launches > 0
                """, ((config, name) for name in names))
            else:
                db.executemany("""
                    INSERT INTO group_usage (config, name, launches) VALUES (?, ?, 1)
                    ON CONFLICT (config, name) DO UPDATE SET launches = launches + 1
                """, ((config, name) for name in names))
                # Keep earlier launches so a failed exec can remove only its own selection.
                return db.execute("""
                    INSERT INTO project_launches (config, project, groups) VALUES (?, ?, ?)
                """, (config, project, json.dumps(names))).lastrowid
    except (OSError, sqlite3.Error, UnicodeError):
        return None  # Launch history is optional.


# Validators take (key, value) and return an error message, or None when valid.

def check_command(key, value):
    if not (isinstance(value, str) or
            isinstance(value, list) and all(isinstance(arg, str) for arg in value)):
        return f"'{key}' must be a string or a list of strings"


def check_bool(key, value):
    if not isinstance(value, bool):
        return f"'{key}' must be true or false"


def check_allowlist(key, value):
    if not isinstance(value, list):
        return f"'{key}' must be a list of rule strings"
    for entry in value:
        allowlist_rule(entry)  # Fails with its own message.


def check_random_ports(key, value):
    if not isinstance(value, list):
        return f"'{key}' must be a list of environment variable names"
    for variable in value:
        if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            return f"invalid random port variable: {variable!r}"


def check_secrets(key, value):
    for secret, spec in value.items():
        if not isinstance(spec, dict) or not spec.get("hosts"):
            return f"secret {secret} needs a 'hosts' list"
        unknown = set(spec) - SECRET_KEYS
        if unknown:
            return f"secret {secret}: unknown key(s): {', '.join(sorted(unknown))}"
        if "cmd" in spec and (not isinstance(spec["cmd"], str) or not spec["cmd"].strip()):
            return f"secret {secret}: 'cmd' must be a non-empty string"


# How groups combine each key: lists are appended, tables merged (later keys
# win), single values replaced. The validator runs on each group's own value.
LIST, TABLE, VALUE = "list", "table", "value"
KEYS = {
    "include": (LIST, None),
    "mounts": (LIST, None),
    "volumes": (LIST, None),
    "ports": (LIST, None),
    "host_ports": (LIST, None),
    "args": (LIST, None),
    "allowlist": (LIST, check_allowlist),
    "random_ports": (LIST, check_random_ports),
    "env": (TABLE, None),
    "secrets": (TABLE, check_secrets),
    "image": (VALUE, None),
    "command": (VALUE, check_command),
    "cpus": (VALUE, None),
    "memory": (VALUE, None),
    "git_metadata": (VALUE, None),
    "tls_intercept": (VALUE, check_bool),
    "net_strict": (VALUE, check_bool),
}


def load_groups(path):
    try:
        with open(path, "rb") as file:
            groups = tomllib.load(file)
    except FileNotFoundError:
        fail(f"config file not found: {path} (use --config)")
    except tomllib.TOMLDecodeError as error:
        fail(f"{path}: {error}")
    for name, group in groups.items():
        if not isinstance(group, dict):
            fail(f"{path}: top-level key {name!r} must be a [group] table")
        unknown = set(group) - set(KEYS)
        if unknown:
            fail(f"group [{name}]: unknown key(s): {', '.join(sorted(unknown))}")
        for key, (_, check) in KEYS.items():
            if key in group and check and (message := check(key, group[key])):
                fail(f"group [{name}]: {message}")
    return groups


def resolve(groups, selected, *, strict=True):
    """Return groups in application order. A non-strict preview skips missing groups and cycles."""
    order, visiting = [], set()

    def visit(name):
        if name in order:
            return
        if name not in groups:
            if strict:
                fail(f"unknown group: {name}")
            return
        if name in visiting:
            if strict:
                fail(f"include cycle through group: {name}")
            return
        visiting.add(name)
        for included in groups[name].get("include", []):
            visit(included)
        order.append(name)

    for name in ([DEFAULT_GROUP] if DEFAULT_GROUP in groups else []) + selected:
        visit(name)
    return order


def merge(groups, names):
    """Combine groups in order, following each key's kind in KEYS."""
    merged = {key: [] if kind == LIST else {} for key, (kind, _) in KEYS.items() if kind != VALUE}
    for name in names:
        for key, value in groups[name].items():
            kind = KEYS[key][0]
            if kind == LIST:
                merged[key] += value
            elif kind == TABLE:
                merged[key] |= value
            else:
                merged[key] = value
    return merged


def expand(text):
    """Expand ~ and host variables written as $$VAR or $${VAR}."""
    def replace(match):
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            fail(f"unset variable in {text!r}")
        return os.environ[name]

    return re.sub(r"\$\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
                  replace, os.path.expanduser(str(text)))


def create_directory(path):
    """Ask whether to create a missing mount source; fail if declined or not interactive."""
    if not sys.stdin.isatty():
        fail(f"mount source does not exist: {path}")
    answer = input(f"mount source does not exist: {path}\nCreate it? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        fail(f"mount source does not exist: {path}")
    os.makedirs(path)


def mount_flags(spec, debug):
    """'SRC[:DST[:OPTIONS]]' -> msb flags; DST defaults to SRC."""
    source, _, rest = expand(spec).partition(":")
    target, _, options = rest.partition(":")
    mapping = ":".join(part for part in (source, target or source, options) if part)
    if not os.path.exists(source):
        if debug:
            print(f"# warning: mount source does not exist: {source}", file=sys.stderr)
        else:
            create_directory(source)
    return ["--mount-file" if os.path.isfile(source) else "-v", mapping]


def allowlist_rule(entry):
    """Add allow@, infer TCP for TARGET:PORT[-PORT], and default a bare TARGET to tcp:443 (except dns)."""
    if not isinstance(entry, str):
        fail("allowlist entries must be non-empty strings")
    rule = entry.strip().removeprefix("allow@")
    if not rule or "@" in rule or "," in rule or any(char.isspace() for char in rule):
        fail(f"invalid allowlist entry: {entry!r}; use [allow@]TARGET[:PROTOCOL[:PORTS]]")

    if rule.startswith("["):
        target, close, rest = rule.partition("]")
        if not close or (rest and not rest.startswith(":")):
            fail(f"invalid allowlist entry: {entry!r}; use [IPv6][:PROTOCOL[:PORTS]]")
        target += close
        qualifiers = rest[1:]
    else:
        target, _, qualifiers = rule.partition(":")
    if not target:
        fail(f"invalid allowlist entry: {entry!r}; target is empty")
    if qualifiers and all(part.isdecimal() for part in qualifiers.split("-", 1)):
        rule = f"{target}:tcp:{qualifiers}"
    elif not qualifiers and target != "dns":
        rule = f"{target}:tcp:443"
    return f"allow@{rule}"


def allowlist_flags(entries):
    """Deny unlisted outbound traffic; domain rules also allow their own DNS lookups."""
    flags = ["--net-default-egress", "deny"]
    for entry in entries:
        flags += ["--net-rule", allowlist_rule(entry)]
    return flags


def network_flags(settings, filtered):
    """Network policy flags; `filtered` applies the allowlist (a selected group set one)."""
    flags = allowlist_flags(settings["allowlist"]) if filtered else []
    for port in settings["host_ports"]:
        flags += ["--net-rule", f"allow@host:tcp:{port}"]
    if settings.get("tls_intercept"):
        flags += ["--tls-intercept"]
    if "net_strict" in settings:
        flags += [f"--net-strict={str(settings['net_strict']).lower()}"]
    return flags


def fixed_tcp_ports(specs):
    """Collect configured host TCP ports, including ranges, to avoid reusing them."""
    ports = set()
    for spec in specs:
        mapping, _, protocol = str(spec).partition("/")
        if protocol not in ("", "tcp") or ":" not in mapping:
            continue
        host = mapping.rsplit(":", 2)[-2]
        start, _, end = host.partition("-")
        try:
            first, last = int(start), int(end or start)
        except ValueError:
            continue  # msb reports malformed fixed mappings.
        if 1 <= first <= last <= 65535:
            ports.update(range(first, last + 1))
    return ports


def choose_random_ports(names, published_ports):
    """Choose distinct high TCP ports without checking or reserving host sockets."""
    names = list(dict.fromkeys(names))
    if not names:
        return {}
    excluded = fixed_tcp_ports(published_ports)
    candidates = [port for port in range(49152, 65536) if port not in excluded]
    if len(names) > len(candidates):
        fail("not enough random TCP ports outside the fixed port mappings")
    return dict(zip(names, map(str, random.sample(candidates, len(names)))))


def git_common_dir(path):
    """The repository metadata directory of a linked worktree, when it lives outside `path`."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=False, capture_output=True, text=True,
    )
    common = result.stdout.strip()
    if result.returncode != 0 or not common or Path(common).is_relative_to(path):
        return None
    return common


def image_cached(image):
    """Whether msb already has the image locally (then it won't pull it)."""
    try:
        result = subprocess.run(["msb", "image", "inspect", image], check=False, capture_output=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def secret_flag(name, spec):
    headers = spec.get("headers", [])
    options = ""
    if len(headers) == 1:
        options = f":headers={headers[0]}"
    elif headers:
        options = f":headers=[{','.join(headers)}]"
    return ["--secret", f"{name}{options}@{','.join(spec['hosts'])}"]


def secret_command(name, spec):
    """The expanded argument list of a secret's 'cmd' entry."""
    try:
        command = [expand(arg) for arg in shlex.split(spec["cmd"])]
    except ValueError:
        fail(f"secret {name}: invalid command quoting")
    if not command:
        fail(f"secret {name}: command is empty")
    return command


def secret_value(name, spec):
    if "cmd" not in spec:
        if name not in os.environ:
            fail(f"secret {name}: not set in the environment and no 'cmd' entry given")
        return os.environ[name]
    try:
        result = subprocess.run(secret_command(name, spec), check=False, capture_output=True, text=True)
    except OSError:
        fail(f"secret {name}: could not execute command")
    if result.returncode != 0:
        fail(f"secret {name}: command failed with exit status {result.returncode}")
    return result.stdout.splitlines()[0] if result.stdout else ""


def parse_args(argv):
    # Everything after the first "--" is the command to run in the sandbox.
    command = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]

    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    config = pre.parse_known_args(argv)[0].config
    groups = load_groups(config)

    parser = argparse.ArgumentParser(
        prog="sb", allow_abbrev=False,
        description="Compose and run an `msb run` command from TOML groups.",
        epilog="Anything after -- is the command to run in the sandbox.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"groups file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--debug", action="store_true", help="print the msb command instead of running it")
    parser.add_argument("--no-picker", action="store_true", help="use the default project name and group flags")
    parser.add_argument("-s", "--skip-cwd", action="store_true",
                        help="do not mount the current directory by default; work from home instead")
    parser.add_argument("--no-network-filter", action="store_true",
                        help="disable network filtering from configured allowlists")
    parser.add_argument("-p", "--port", dest="ports", action="append", default=[], metavar="PORT",
                        help="publish a port, e.g. 127.0.0.1:3001:3001 (like ports =)")
    parser.add_argument("-v", "--mount", dest="mounts", action="append", default=[], metavar="SRC[:DST[:OPTIONS]]",
                        help="mount a file or directory (like mounts =)")
    parser.add_argument("-e", "--env", dest="env", action="append", default=[], metavar="KEY=VALUE",
                        help="set an environment variable (like env =)")
    parser.set_defaults(groups=[])
    selectable = parser.add_argument_group("groups")
    for name in groups:
        if name == DEFAULT_GROUP:
            continue
        if name in BUILTIN_OPTIONS:
            fail(f"group name {name!r} clashes with a built-in option")
        selectable.add_argument(f"--{name}", dest="groups", action="append_const", const=name,
                                help="preselect this group")
    parser.add_argument("paths", nargs="*", metavar="PATH", help="files or directories to mount (default: .)")
    args = parser.parse_args(argv)
    for item in args.env:
        if "=" not in item:
            parser.error(f"-e/--env expects KEY=VALUE, got {item!r}")
    return args, groups, command


def project_mounts(paths, skip_cwd):
    cwd = str(Path.cwd().resolve())
    if not skip_cwd and cwd == str(Path.home().resolve()):
        fail("cannot run from the home directory without --skip-cwd. Change directory or use --skip-cwd.")
    mounts = list(dict.fromkeys(os.path.realpath(path) for path in paths))
    if not mounts and not skip_cwd:
        mounts = [cwd]
    cwd_only = mounts == [cwd]
    if cwd_only:
        project = os.path.basename(cwd) or "workspace"
    elif mounts:
        paths_bytes = b"\0".join(sorted(map(os.fsencode, mounts)))
        project = hashlib.sha256(paths_bytes).hexdigest()[:16]
    else:
        project = "workspace"
    return mounts, project, cwd_only


def pick_startup(project, names, preselect, groups, usage):
    try:
        from prompt_toolkit.application import Application
        from prompt_toolkit.completion import CompleteEvent, FuzzyCompleter, WordCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.filters import has_focus
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import HSplit, Layout
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import CheckboxList, Label, TextArea
    except ModuleNotFoundError:
        fail("install prompt_toolkit to open the startup picker, or use --no-picker")

    class GroupList(CheckboxList):
        def __init__(self, names):
            self.labels = {name: partial(self.label_for, name) for name in names}
            super().__init__(list(self.labels.items()), select_character="x")
            self.control.get_key_bindings().remove(Keys.Any)  # Use fuzzy search instead.

        def preselect(self, selected):
            self.explicit = list(dict.fromkeys(name for name in selected if name in self.labels))
            self.current_values = resolve(groups, self.explicit, strict=False)
            self.show_matches(names)
            if self.explicit:
                self._selected_index = names.index(self.explicit[0])

        def label_for(self, name):
            label = name if name.isprintable() else repr(name)
            if name in self.current_values and name not in self.explicit:
                return f"{label} (included)"
            return label

        def show_matches(self, names, keep_focus=False):
            focused = self.values[self._selected_index][0] if keep_focus and self.values else None
            self.values = [(name, self.labels[name]) for name in names]
            self._selected_index = names.index(focused) if focused in names else 0

        def _handle_enter(self):
            if not self.values:
                return
            name = self.values[self._selected_index][0]
            if name in self.explicit:
                self.explicit.remove(name)
            else:
                self.explicit.append(name)
            self.current_values = resolve(groups, self.explicit, strict=False)

        def _get_text_fragments(self):
            if not self.values:
                return [("", "No matching groups")]
            return super()._get_text_fragments()

    names = sorted(names, key=lambda name: usage.get(name, 0), reverse=True)
    choices = GroupList(names) if names else None
    if choices is not None:
        choices.preselect(preselect(project))
    completer = FuzzyCompleter(WordCompleter(names, sentence=True), WORD=True)
    query = ""
    default_project = project
    project_field = TextArea(text=project, multiline=False, prompt="Project: ")
    project_focused = has_focus(project_field)
    group_focused = ~project_focused
    keys = KeyBindings()

    def select_project_text():
        buffer = project_field.buffer
        buffer.cursor_position = 0
        buffer.start_selection()
        buffer.cursor_position = len(buffer.text)
        buffer.selection_state.enter_shift_mode()

    select_project_text()

    @keys.add(Keys.BracketedPaste, filter=project_focused)
    def paste_project(event):
        buffer = project_field.buffer
        if buffer.selection_state:
            buffer.cut_selection()
        buffer.insert_text(" ".join(event.data.replace("\x00", "").splitlines()))

    @keys.add("enter", filter=project_focused, eager=True)
    @keys.add("tab", filter=project_focused if choices else False)
    @keys.add("s-tab", filter=project_focused if choices else False)
    def confirm_project(event):
        nonlocal project, query
        name = project_field.text if project_field.text.strip() else default_project
        if name != project_field.text:
            project_field.buffer.document = Document(name, len(name))
        project_field.buffer.exit_selection()
        if choices is None:
            event.app.exit(result=(name, []))
            return
        if name != project:
            project = name
            query = ""
            choices.preselect(preselect(project))
        event.app.layout.focus(choices)

    @keys.add("tab", filter=group_focused)
    @keys.add("s-tab", filter=group_focused)
    def focus_project(event):
        event.app.layout.focus(project_field)
        select_project_text()

    @keys.add(Keys.Any, filter=group_focused)
    def search(event):
        nonlocal query
        if event.data.isprintable():
            query += event.data
            matches = sorted(completer.get_completions(Document(query), CompleteEvent()),
                             key=lambda match: usage.get(match.text, 0), reverse=True)
            choices.show_matches([match.text for match in matches])

    @keys.add(" ", filter=group_focused, eager=True)
    @keys.add("backspace", filter=group_focused, eager=True)
    def toggle_or_clear(event):
        nonlocal query
        if event.data == " ":
            choices._handle_enter()
        if query:
            query = ""
            choices.show_matches(names, keep_focus=True)

    @keys.add("enter", filter=group_focused, eager=True)
    def confirm(event):
        event.app.exit(result=(project, choices.explicit))

    @keys.add("escape", eager=True)
    @keys.add("c-c", eager=True)
    def cancel(event):
        event.app.exit()

    def instructions():
        if choices is None:
            return "Type to replace the name. Enter: launch   Esc: cancel"
        if project_focused():
            return "Type to replace the name. Enter or Tab: go to groups   Esc: cancel"
        return "↑/↓ or j/k: move   Space: toggle   Tab: edit name   Enter: launch   Esc: cancel"

    rows = [project_field, Label(instructions)]
    if choices is not None:
        title = "Select groups ([default] always applies)" if DEFAULT_GROUP in groups else "Select groups"
        rows += [
            Label(title),
            Label(lambda: f"Search: {query}" if query else "Type to search. Backspace clears the search."),
            choices,
        ]
    layout = Layout(HSplit(rows), focused_element=project_field)
    style = Style.from_dict({"checkbox-selected": "reverse"})
    return Application(layout=layout, key_bindings=keys, style=style, erase_when_done=True).run()


def build_command(args, groups, command, paths, project):
    """Return the msb command line and the secrets it needs, without running it.

    It sets SB_PROJECT and random port variables in os.environ for $${VAR}
    expansion, and asks git and msb about worktrees and cached images.
    """
    names = resolve(groups, args.groups)
    settings = merge(groups, names)
    # Command-line additions behave like one more group applied last.
    settings["ports"] += args.ports
    settings["mounts"] += args.mounts
    settings["env"] |= dict(item.split("=", 1) for item in args.env)

    workdir = str(Path.home())
    if args.skip_cwd:
        workdir = next((path for path in paths if os.path.isdir(path)), workdir)
    elif paths:
        ancestor = os.path.commonpath([os.path.dirname(path) if os.path.isfile(path) else path
                                       for path in paths])
        if ancestor != os.path.sep:
            workdir = ancestor
    os.environ["SB_PROJECT"] = project

    if "image" not in settings:
        fail("no image set: add image = \"...\" to the [default] group")

    conflicts = set(settings["random_ports"]) & (set(settings["env"]) | set(settings["secrets"]) | {"SB_PROJECT"})
    if conflicts:
        fail(f"random port variable(s) already defined: {', '.join(sorted(conflicts))}")

    random_ports = choose_random_ports(settings["random_ports"], settings["ports"])
    os.environ.update(random_ports)  # Available to $${VAR} expansion in the configuration.
    settings["env"] |= random_ports
    for port in random_ports.values():
        settings["ports"].append(f"127.0.0.1:{port}:{port}")

    flags = [expand(arg) for arg in settings["args"]] + ["-w", workdir]
    if "cpus" in settings:
        flags += ["--cpus", str(settings["cpus"])]
    if "memory" in settings:
        flags += ["--memory", str(settings["memory"])]
    mounts = paths.copy()
    if settings.get("git_metadata") and not args.skip_cwd:
        mounts += [common for path in paths if os.path.isdir(path)
                   if (common := git_common_dir(path)) and common not in mounts]
    for spec in mounts + settings["mounts"]:
        flags += mount_flags(spec, args.debug)
    for spec in settings["volumes"]:
        flags += ["--mount-named", expand(spec)]
    for key, value in settings["env"].items():
        flags += ["-e", f"{key}={expand(value)}"]
    for port in settings["ports"]:
        flags += ["-p", str(port)]
    filtered = not args.no_network_filter and any("allowlist" in groups[name] for name in names)
    flags += network_flags(settings, filtered)
    for name, spec in settings["secrets"].items():
        flags += secret_flag(name, spec)

    # Hide the progress display unless the image actually has to be pulled.
    quiet = ["--quiet"] if image_cached(settings["image"]) else []
    cmd = ["msb", "run", *quiet, *flags, settings["image"]]
    if not command:
        configured_command = settings.get("command", [])
        if isinstance(configured_command, str):
            command = ["sh", "-c", expand(configured_command)]
        else:
            command = [expand(part) for part in configured_command]
    if command:
        cmd += ["--", *command]
    return cmd, settings["secrets"]


def main(argv):
    args, groups, command = parse_args(argv)
    paths, project, cwd_only = project_mounts(args.paths, args.skip_cwd)
    state_path = state_database_path()
    config = Path(os.path.realpath(args.config)).as_uri()
    if not args.no_picker and sys.stdin.isatty() and sys.stdout.isatty():
        always_applied = set(resolve(groups, []))
        names = [name for name in groups if name not in always_applied]
        flags = dict.fromkeys(args.groups)

        def preselect(name):
            saved = load_project_groups(state_path, config, name, inherit=cwd_only)
            return [group for group in saved if group not in flags] + list(flags)

        selection = pick_startup(project, names, preselect, groups, load_group_usage(state_path, config))
        if selection is None:
            sys.exit(130)
        project, args.groups = selection
    cmd, secrets = build_command(args, groups, command, paths, project)

    if args.debug:
        # A runnable script: secrets are read like sb does (first output line), not here.
        for name, spec in secrets.items():
            if "cmd" in spec:
                print(f'export {name}="$({shlex.join(secret_command(name, spec))} | head -n 1)"')
            else:
                print(f"# {name} from host environment")
        print(shlex.join(cmd))
        return

    env = os.environ | {name: secret_value(name, spec) for name, spec in secrets.items()}
    selected = list(dict.fromkeys(args.groups))
    launch = None
    if shutil.which("msb", path=env.get("PATH", os.defpath)):
        launch = update_launch_state(state_path, config, project, selected)
    try:
        os.execvpe("msb", cmd, env)
    except BaseException as error:
        # A successful exec replaces this process. Undo the record if the handoff fails.
        if launch is not None:
            update_launch_state(state_path, config, project, selected, undo=launch)
        if isinstance(error, FileNotFoundError):
            fail("msb not found on PATH")
        raise


if __name__ == "__main__":
    main(sys.argv[1:])
