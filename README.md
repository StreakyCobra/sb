# sb

Personal tooling to run coding agents (Claude Code, opencode) in throwaway
[microsandbox](https://github.com/superradcompany/microsandbox) microVMs.
`sb` composes an `msb run` command from groups in a TOML file.

## Setup

Requires KVM, Podman, `pass` and Python 3.11+. The group picker uses Prompt Toolkit 3.

```bash
# Group picker on Arch Linux
sudo pacman -S python-prompt_toolkit

# msb, packaged with its matching libkrunfw
(cd microsandbox-bundle && makepkg -si)

# Image (rebuild with --no-cache to update Claude Code and opencode)
podman build -t localhost/sb . && podman save localhost/sb:latest | msb load

# Composer and groups
ln -s "$PWD/sb.py" ~/.local/bin/sb
mkdir -p ~/.config/sb ~/.local/share/sb/{claude,opencode}
cp example.toml ~/.config/sb/config.toml
```

## Usage

```bash
sb                                       # choose groups and launch the sandbox
sb -i                                    # pick and launch a recent project
sb --claude -- claude --continue         # Claude Code, resuming the last session
sb --opencode --github ~/Workspace/proj  # opencode with a GitHub token
sb --debug --claude --java .             # print the msb command instead of running it
sb --no-picker --java                   # launch with Java without opening the picker
sb --claude -p 127.0.0.1:3001:3001       # extra port (also -v SRC[:DST] and -e KEY=VALUE)
sb -s                                    # work from sandbox home without mounting the current directory
sb --no-network-filter                   # ignore configured allowlists for this run
sb --help                                # list the groups
```

Run `sb -i` to pick a saved project for the current configuration file. The
launcher lists the most recently used projects first. Type to filter by name
or path. Use the arrow keys or `j` and `k` to move. Press Backspace to clear the
search, Enter to launch with the saved paths and groups, or Esc to cancel.

You can add group flags or pass a command for this run. Projects saved before
the launcher was added appear after you launch them once without `-i`.

In a terminal, `sb` opens the startup picker with the project name selected.
Type to replace the name, or press Enter to keep it and move to the groups.
Press Tab to switch between the name and the groups. If there are no optional
groups, press Enter to launch from the name field.

The picker selects the groups from the last launch with the same project
paths. Renaming a project keeps its groups. Flags such as `--java` also select
groups. For new paths, the picker can use the latest selection with the same
project name. If the current folder is the only project path, it also tries
the name before the first `--`. For example, `swan-lake-infra--implement-probes`
starts with the groups for `swan-lake-infra` until it has its own saved selection.

The picker hides `[default]` and every group it includes, even through other
groups. These groups always apply.

- Use the arrow keys or `j` and `k` to move.
- Type other characters to filter by letters in order. For example, `pythn` matches
  `python`. Use `J` or `K` to search for those letters.
- Press Space to toggle the highlighted group and clear the search.
- Press Backspace to clear the entire search. Checked groups stay checked while you search.
- Press Enter to launch, or Esc to cancel.

When you check a group, the picker also checks its included groups and marks them as `(included)`.
Press Space on an included group to keep it checked on its own.

With `--no-picker` or redirected input or output, `sb` uses the saved or
default project name and the group flags. Groups you select directly apply in
selection order, with their includes first. Flagged groups apply last, in flag order.

The picker lists the most-used groups first, even during a search. `sb` counts
each group you select directly once per `msb` launch. It also saves that
selection, even if it is empty. Launches with `--no-picker` count. Canceling
or using `--debug` leaves the saved state unchanged. Each configuration file
has its own state in `$XDG_STATE_HOME/sb/state.sqlite3` (default:
`~/.local/state/sb/state.sqlite3`).

`SB_PROJECT` is the name from the startup picker. When you give a set of
project paths a name and launch, `sb` prefills that name for the same paths
next time, even in a different order.

If there is no saved name, the current folder's name is the default when it
is the only project path. Other `PATH` arguments produce a short hash of their
absolute paths. The order of the paths does not change the hash. With `-s` and
no paths, the default name is `workspace`.

Paths are mounted at the same location as on the host. The sandbox starts in
their deepest shared directory, using each file's parent directory. This does
not mount any additional host paths. If the shared directory is `/`, the sandbox
starts in home. With `-s`, it starts in the first explicit directory or in home.
The `-s` option also disables automatic `git_metadata` mounts, even for explicit
paths. Secrets are read from `pass` (or the environment) and injected by msb on
the way out: the sandbox only sees placeholders. See `example.toml` for the group
syntax.

`sb` asks before it creates a missing mount source. To skip confirmation
for a directory and its subdirectories, add `auto_create_mount_dirs` to
`[default]` or to a group such as `[opencode]`:

```toml
auto_create_mount_dirs = ["~/.local/share/sb/opencode"]
```

The list applies to host paths. Paths outside the listed directories still
require confirmation. `--debug` reports missing sources without creating them.

Configured commands can be strings, evaluated by `sh -c` inside the sandbox:

```toml
command = 'exec codex -c "projects.\"$(git rev-parse --show-toplevel)\".trust_level=trusted"'
```

`sb` expands `$$VAR` and `$${VAR}` on the host. A single `$` passes through
unchanged. Update existing configurations that use `$VAR` or `${VAR}` for host
expansion to use the doubled-dollar syntax.
Use an argument list instead of a string to run a command without a shell.

Sandboxes set `SB_SANDBOX=msb`. starship can't detect a microVM as a
container, so show the variable instead, in `~/.config/starship.toml`:

```toml
# Second line, just before the ❯ ($all still includes the line break).
format = "$all${env_var.SB_SANDBOX}$character"

[env_var.SB_SANDBOX]
format = "[\\[$env_value\\]]($style) "
style = "bold blue"
```

## Known limitations (msb 0.8.1)

- Binaries can't be executed from mounted folders, so the tools live in the
  image and self-updates are disabled.
- File locks don't cross VMs: opencode uses one database per project.
