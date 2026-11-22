# sb

Personal tooling to run coding agents (Claude Code, opencode) in throwaway
[microsandbox](https://github.com/superradcompany/microsandbox) microVMs.
`sb` composes an `msb run` command from groups in a TOML file.

## Setup

Requires KVM, Podman, `pass` and Python 3.11+.

```bash
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
sb                                       # shell in the current directory
sb --claude -- claude --continue         # Claude Code, resuming the last session
sb --opencode --github ~/Workspace/proj  # opencode with a GitHub token
sb --debug --claude --java .             # print the msb command instead of running it
sb --claude -p 127.0.0.1:3001:3001       # extra port (also -v SRC[:DST] and -e KEY=VALUE)
sb -s                                    # work from sandbox home without mounting the current directory
sb --no-network-filter                   # ignore configured allowlists for this run
sb --help                                # list the groups
```

Paths are mounted at the same location as on the host; the first directory is
the working directory. `SB_PROJECT` is the working directory's name, or
`workspace` when `-s` is used. The `-s` option also disables automatic
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

## Known limitations (msb 0.7.16)

- Binaries can't be executed from mounted folders, so the tools live in the
  image and self-updates are disabled.
- File locks don't cross VMs: opencode uses one database per project.
