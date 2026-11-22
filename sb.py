#!/usr/bin/env python3
"""sb: compose an `msb run` command from groups defined in a TOML file.

    sb [--config FILE] [--debug] [-s] [--no-network-filter] [-p PORT] [-v MOUNT] [-e KEY=VALUE]
       [--GROUP ...] [PATH ...] [-- COMMAND ...]

Each PATH (default: the current directory) is mounted at the same absolute path
in the sandbox; the first directory becomes the working directory. The group
named "default" is always applied; other groups are selected with --GROUP.
-p, -v and -e add to the ports, mounts and env of the selected groups.
See example.toml for the file format.
"""

import argparse
import os
import random
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

DEFAULT_GROUP = "default"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
DEFAULT_CONFIG = CONFIG_HOME / "sb" / "config.toml"

LIST_KEYS = {"include", "mounts", "volumes", "ports", "host_ports", "args", "allowlist", "random_ports"}
DICT_KEYS = {"env", "secrets"}
BOOL_KEYS = {"tls_intercept", "net_strict"}
OTHER_KEYS = {"image", "command", "cpus", "memory", "git_metadata"} | BOOL_KEYS
SECRET_KEYS = {"cmd", "hosts", "headers"}
BUILTIN_OPTIONS = {"config", "debug", "help", "port", "mount", "env", "skip-cwd", "no-network-filter"}


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
        if "command" in group:
            command = group["command"]
            if not (isinstance(command, str) or
                    isinstance(command, list) and all(isinstance(arg, str) for arg in command)):
                fail(f"group [{name}]: 'command' must be a string or a list of strings")
        for key in sorted(BOOL_KEYS & set(group)):
            if not isinstance(group[key], bool):
                fail(f"group [{name}]: '{key}' must be true or false")
        if "allowlist" in group:
            if not isinstance(group["allowlist"], list):
                fail(f"group [{name}]: 'allowlist' must be a list of rule strings")
            for entry in group["allowlist"]:
                allowlist_rule(entry)
        if "random_ports" in group:
            if not isinstance(group["random_ports"], list):
                fail(f"group [{name}]: 'random_ports' must be a list of environment variable names")
            for variable in group["random_ports"]:
                if not isinstance(variable, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                    fail(f"group [{name}]: invalid random port variable: {variable!r}")
        for secret, spec in group.get("secrets", {}).items():
            if not isinstance(spec, dict) or not spec.get("hosts"):
                fail(f"group [{name}]: secret {secret} needs a 'hosts' list")
            unknown = set(spec) - SECRET_KEYS
            if unknown:
                fail(f"group [{name}]: secret {secret}: unknown key(s): {', '.join(sorted(unknown))}")
            if "cmd" in spec and (not isinstance(spec["cmd"], str) or not spec["cmd"].strip()):
                fail(f"group [{name}]: secret {secret}: 'cmd' must be a non-empty string")
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
    """Expand ~ and host variables written as $$VAR or $${VAR}."""
    def replace(match):
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            fail(f"unset variable in {text!r}")
        return os.environ[name]

    return re.sub(r"\$\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))",
                  replace, os.path.expanduser(str(text)))


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


def allowlist_rule(entry):
    """Add allow@ and infer TCP for TARGET:PORT or TARGET:PORT-RANGE."""
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
    return f"allow@{rule}"


def allowlist_flags(entries):
    """Deny unlisted outbound traffic; domain rules also allow their own DNS lookups."""
    flags = ["--net-default-egress", "deny"]
    for entry in entries:
        flags += ["--net-rule", allowlist_rule(entry)]
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


def secret_value(name, spec):
    if "cmd" not in spec:
        if name not in os.environ:
            fail(f"secret {name}: not set in the environment and no 'cmd' entry given")
        return os.environ[name]
    try:
        command = [expand(arg) for arg in shlex.split(spec["cmd"])]
        if not command:
            fail(f"secret {name}: command is empty")
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except ValueError:
        fail(f"secret {name}: invalid command quoting")
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
        selectable.add_argument(f"--{name}", dest="groups", action="append_const", const=name)
    parser.add_argument("paths", nargs="*", metavar="PATH", help="files or directories to mount (default: .)")
    args = parser.parse_args(argv)
    for item in args.env:
        if "=" not in item:
            parser.error(f"-e/--env expects KEY=VALUE, got {item!r}")
    return args, groups, command


def main(argv):
    args, groups, command = parse_args(argv)
    if not args.skip_cwd and Path.cwd().resolve() == Path.home().resolve():
        fail("cannot run from the home directory without --skip-cwd. Change directory or use --skip-cwd.")
    names = resolve(groups, args.groups)
    settings = merge(groups, names)
    # Command-line additions behave like one more group applied last.
    settings["ports"] += args.ports
    settings["mounts"] += args.mounts
    settings["env"] |= dict(item.split("=", 1) for item in args.env)

    # Working directory: the first directory passed; the current one when no path
    # is given; the home directory when only files are given (nothing else is mounted).
    paths = [os.path.realpath(path) for path in args.paths]
    if not paths and not args.skip_cwd:
        paths = [os.getcwd()]
    workdir = next((path for path in paths if os.path.isdir(path)), str(Path.home()))
    os.environ["SB_PROJECT"] = os.path.basename(workdir)  # usable as $${SB_PROJECT} in the TOML

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
    if not args.no_network_filter and any("allowlist" in groups[name] for name in names):
        flags += allowlist_flags(settings["allowlist"])
    for port in settings["host_ports"]:
        flags += ["--net-rule", f"allow@host:tcp:{port}"]
    if settings.get("tls_intercept"):
        flags += ["--tls-intercept"]
    if "net_strict" in settings:
        flags += [f"--net-strict={str(settings['net_strict']).lower()}"]
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

    if args.debug:
        for name, spec in settings["secrets"].items():
            source = f"command: {spec['cmd']}" if "cmd" in spec else "host environment"
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
