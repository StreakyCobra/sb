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
sb --claude -- claude --continue         # Claude Code, resuming the last session
sb --opencode --github ~/Workspace/proj  # opencode with a GitHub token
sb --debug --claude --java .             # print the msb command instead of running it
sb --no-picker --java                   # launch with Java without opening the picker
sb --claude -p 127.0.0.1:3001:3001       # extra port (also -v SRC[:DST] and -e KEY=VALUE)
sb -s                                    # work from sandbox home without mounting the current directory
sb --no-network-filter                   # ignore configured allowlists for this run
sb --help                                # list the groups
```

In a terminal, `sb` opens the group picker. Flags such as `--java` check those
groups in advance. The `[default]` group always applies if it exists.

- Use the arrow keys or `j` and `k` to move.
- Type other characters to filter by letters in order. For example, `pythn` matches
  `python`. Use `J` or `K` to search for those letters.
- Press Space to toggle the highlighted group and clear the search.
- Press Backspace to clear the entire search. Checked groups stay checked while you search.
- Press Enter to confirm, or Esc to cancel.

If input or output is redirected, `sb` skips the picker and uses the group flags directly.
Groups apply in the order you check them. Groups from flags start in flag order.

Paths are mounted at the same location as on the host. The sandbox starts in
their deepest shared directory, using each file's parent directory. This does
not mount any additional host paths. `SB_PROJECT` is the starting directory's
name. If the shared directory is `/`, the sandbox starts in home and uses
`SB_PROJECT=workspace`. With `-s`, it starts in the first explicit directory
or in home and always uses `SB_PROJECT=workspace`. The `-s` option also disables automatic
`git_metadata` mounts, even for explicit paths. Secrets are read from `pass` (or
the environment) and injected by msb on the way out: the sandbox only sees placeholders. See
`example.toml` for the group syntax.

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
