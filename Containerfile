FROM docker.io/library/archlinux:base

RUN --mount=type=cache,id=pacman-pkg,target=/var/cache/pacman/pkg,sharing=locked \
    pacman -Syu --noconfirm --needed \
    curl \
    foot-terminfo \
    git \
    ripgrep \
    starship \
    sudo \
    zsh \
    && rm -rf /var/lib/pacman/sync/* \
    && useradd --create-home --uid 1000 --user-group --shell /bin/zsh fabien \
    && echo 'fabien ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/fabien \
    && chmod 0440 /etc/sudoers.d/fabien


USER fabien
WORKDIR /home/fabien
ENV HOME=/home/fabien \
    PATH=/home/fabien/.local/bin:$PATH

# Pre-create common parents as the user: msb creates missing parents of mount
# points as root, which would leave them unwritable.
RUN mkdir -p ~/.config ~/.cache ~/.local/{bin,share,state} \
    && echo 'eval "$(starship init zsh)"' > ~/.zshrc \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path

CMD ["/bin/zsh"]
