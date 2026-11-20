#!/usr/bin/env python3
"""sb: compose an `msb run` command from groups defined in a TOML file.

    sb [--config FILE] [--debug] [-p PORT] [-v MOUNT] [-e KEY=VALUE]
       [--GROUP ...] [PATH ...] [-- COMMAND ...]

Each PATH (default: the current directory) is mounted at the same absolute path
in the sandbox; the first directory becomes the working directory. The group
named "default" is always applied; other groups are selected with --GROUP.
-p, -v and -e add to the ports, mounts and env of the selected groups.
See example.toml for the file format.
"""

import argparse
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_GROUP = "default"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
DEFAULT_CONFIG = CONFIG_HOME / "sb" / "config.toml"

LIST_KEYS = {"include", "mounts", "volumes", "ports", "host_ports", "args"}
DICT_KEYS = {"env", "secrets"}
OTHER_KEYS = {"description", "image", "command", "cpus", "memory", "git_metadata"}
SECRET_KEYS = {"pass", "hosts", "headers"}
BUILTIN_OPTIONS = {"config", "debug", "help", "port", "mount", "env"}


def fail(message):
    sys.exit(f"sb: {message}")


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
        unknown = set(group) - LIST_KEYS - DICT_KEYS - OTHER_KEYS
        if unknown:
            fail(f"group [{name}]: unknown key(s): {', '.join(sorted(unknown))}")
        for secret, spec in group.get("secrets", {}).items():
            if not isinstance(spec, dict) or not spec.get("hosts"):
                fail(f"group [{name}]: secret {secret} needs a 'hosts' list")
            unknown = set(spec) - SECRET_KEYS
            if unknown:
                fail(f"group [{name}]: secret {secret}: unknown key(s): {', '.join(sorted(unknown))}")
    return groups


def resolve(groups, selected):
    """Return group names in application order: includes before the group itself, each once."""
    order, visiting = [], set()

    def visit(name):
        if name in order:
            return
        if name not in groups:
            fail(f"unknown group: {name}")
        if name in visiting:
            fail(f"include cycle through group: {name}")
        visiting.add(name)
        for included in groups[name].get("include", []):
            visit(included)
        order.append(name)

    for name in ([DEFAULT_GROUP] if DEFAULT_GROUP in groups else []) + selected:
        visit(name)
    return order


def merge(groups, names):
    """Lists are appended, tables merged (later keys win), single values replaced."""
    merged = {key: [] for key in LIST_KEYS} | {key: {} for key in DICT_KEYS}
    for name in names:
        for key, value in groups[name].items():
            if key in LIST_KEYS:
                merged[key] += value
            elif key in DICT_KEYS:
                merged[key] |= value
            else:
                merged[key] = value
    return merged


def expand(text):
    """Expand ~ and $VAR / ${VAR}; an unset variable is an error."""
    result = os.path.expandvars(os.path.expanduser(str(text)))
    if "$" in result:
        fail(f"unset variable in {text!r}")
    return result


def mount_flags(spec, debug):
    """'SRC[:DST[:OPTIONS]]' -> msb flags; DST defaults to SRC."""
    source, _, rest = expand(spec).partition(":")
    target, _, options = rest.partition(":")
    mapping = ":".join(part for part in (source, target or source, options) if part)
    if not os.path.exists(source):
        if not debug:
            fail(f"mount source does not exist: {source}")
        print(f"# warning: mount source does not exist: {source}", file=sys.stderr)
    return ["--mount-file" if os.path.isfile(source) else "-v", mapping]


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


def secret_value(name, spec):
    if "pass" not in spec:
        if name not in os.environ:
            fail(f"secret {name}: not set in the environment and no 'pass' entry given")
        return os.environ[name]
    result = subprocess.run(["pass", "show", spec["pass"]], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"secret {name}: pass show {spec['pass']} failed: {result.stderr.strip()}")
    return result.stdout.splitlines()[0] if result.stdout else ""


def parse_args(argv):
    # Everything after the first "--" is the command to run in the sandbox.
    command = []
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]

    pre = argparse.ArgumentParser(add_help=False)
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
    parser.add_argument("-p", "--port", dest="ports", action="append", default=[], metavar="PORT",
                        help="publish a port, e.g. 127.0.0.1:3001:3001 (like ports =)")
    parser.add_argument("-v", "--mount", dest="mounts", action="append", default=[], metavar="SRC[:DST[:OPTIONS]]",
                        help="mount a file or directory (like mounts =)")
    parser.add_argument("-e", "--env", dest="env", action="append", default=[], metavar="KEY=VALUE",
                        help="set an environment variable (like env =)")
    parser.set_defaults(groups=[])
    selectable = parser.add_argument_group("groups")
    for name, group in groups.items():
        if name == DEFAULT_GROUP:
            continue
        if name in BUILTIN_OPTIONS:
            fail(f"group name {name!r} clashes with a built-in option")
        selectable.add_argument(f"--{name}", dest="groups", action="append_const", const=name,
                                help=group.get("description"))
    parser.add_argument("paths", nargs="*", metavar="PATH", help="files or directories to mount (default: .)")
    args = parser.parse_args(argv)
    for item in args.env:
        if "=" not in item:
            parser.error(f"-e/--env expects KEY=VALUE, got {item!r}")
    return args, groups, command


def main(argv):
    args, groups, command = parse_args(argv)
    settings = merge(groups, resolve(groups, args.groups))
    # Command-line additions behave like one more group applied last.
    settings["ports"] += args.ports
    settings["mounts"] += args.mounts
    settings["env"] |= dict(item.split("=", 1) for item in args.env)

    # Working directory: the first directory passed; the current one when no path
    # is given; the home directory when only files are given (nothing else is mounted).
    paths = [os.path.realpath(path) for path in args.paths] or [os.getcwd()]
    workdir = next((path for path in paths if os.path.isdir(path)), str(Path.home()))
    os.environ["SB_PROJECT"] = os.path.basename(workdir)  # usable as ${SB_PROJECT} in the TOML

    if "image" not in settings:
        fail("no image set: add image = \"...\" to the [default] group")

    flags = [expand(arg) for arg in settings["args"]] + ["-w", workdir]
    if "cpus" in settings:
        flags += ["--cpus", str(settings["cpus"])]
    if "memory" in settings:
        flags += ["--memory", str(settings["memory"])]
    mounts = list(dict.fromkeys(paths))
    if settings.get("git_metadata"):
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
    for port in settings["host_ports"]:
        flags += ["--net-rule", f"allow@host:tcp:{port}"]
    for name, spec in settings["secrets"].items():
        flags += secret_flag(name, spec)

    # Hide the progress display unless the image actually has to be pulled.
    quiet = ["--quiet"] if image_cached(settings["image"]) else []
    cmd = ["msb", "run", *quiet, *flags, settings["image"]]
    command = command or [expand(part) for part in settings.get("command", [])]
    if command:
        cmd += ["--", *command]

    if args.debug:
        for name, spec in settings["secrets"].items():
            source = f"pass show {spec['pass']}" if "pass" in spec else "host environment"
            print(f"# {name} from {source}")
        print(shlex.join(cmd))
        return

    env = os.environ | {name: secret_value(name, spec) for name, spec in settings["secrets"].items()}
    try:
        os.execvpe("msb", cmd, env)
    except FileNotFoundError:
        fail("msb not found on PATH")


if __name__ == "__main__":
    main(sys.argv[1:])
