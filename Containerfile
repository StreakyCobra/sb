FROM docker.io/library/archlinux:base

# Keep these in sync with the pacman allowlist in example.toml.
RUN printf '%s\n' \
    'Server = https://mirror.puzzle.ch/archlinux/$repo/os/$arch' \
    'Server = https://mirror.init7.net/archlinux/$repo/os/$arch' \
    > /etc/pacman.d/mirrorlist

RUN --mount=type=cache,id=pacman-pkg,target=/var/cache/pacman/pkg,sharing=locked \
    pacman -Syu --noconfirm --needed \
    bubblewrap \
    curl \
    direnv \
    docker \
    docker-compose \
    foot-terminfo \
    git \
    nodejs \
    npm \
    ripgrep \
    starship \
    sudo \
    zsh \
    zoxide \
    && rm -rf /var/lib/pacman/sync/* \
    && useradd --create-home --uid 1000 --user-group --shell /bin/zsh fabien \
    && echo 'fabien ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/fabien \
    && chmod 0440 /etc/sudoers.d/fabien


USER fabien
WORKDIR /home/fabien
ENV HOME=/home/fabien \
    PATH=/home/fabien/.opencode/bin:/home/fabien/.local/bin:$PATH

# Pre-create common parents as the user: msb creates missing parents of mount
# points as root, which would leave them unwritable.
RUN mkdir -p ~/.config ~/.cache ~/.local/{bin,share,state} ~/.codex \
    && echo 'eval "$(starship init zsh)"' > ~/.zshrc \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path \
    && curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/sb-install-codex.sh \
    && CODEX_NON_INTERACTIVE=1 CODEX_HOME="$HOME/.local/share/codex-cli" sh /tmp/sb-install-codex.sh \
    && rm /tmp/sb-install-codex.sh

CMD ["/bin/zsh"]
