#!/usr/bin/env bash
# Dev container bootstrap, run by postCreateCommand as the `vscode` user
# (passwordless sudo available). Installs nix single-user (there is no
# reliable nix devcontainer feature), then warms the flake dev shell so the
# first open pays the store download once, here, instead of in a terminal.
set -euo pipefail

if [ ! -x "$HOME/.nix-profile/bin/nix" ]; then
    echo ">>> installing nix (single-user, no daemon)"
    # Upstream installer; pipe form (not <(...)) keeps it POSIX-sh safe.
    if ! curl -L https://nixos.org/nix/install | sh -s -- --no-daemon; then
        echo ">>> upstream installer failed, falling back to Determinate"
        curl -sSfL https://install.determinate.systems/nix \
            | sh -s -- install linux --init none --no-confirm
    fi
fi

# nix on PATH for this script (remoteEnv only reaches VS Code processes).
. "$HOME/.nix-profile/etc/profile.d/nix.sh"
nix --version

# Fresh nix disables flakes; the repo needs `nix develop` (nix-command).
NIX_CONF="$HOME/.config/nix/nix.conf"
mkdir -p "$(dirname "$NIX_CONF")"
if ! grep -qs '^experimental-features' "$NIX_CONF"; then
    printf 'experimental-features = nix-command flakes\n' >> "$NIX_CONF"
fi

# Warm the flake shell and prove the toolchain end-to-end: `clab` must come
# from the flake (nixpkgs ships the binary as `containerlab`; the flake
# exposes the conventional `clab` name).
#
# `path:.` (not bare `.`): this repo uses wtree worktrees whose `.git` file
# points at an absolute host path (`../../.bare/worktrees/...`) that does not
# exist inside the container; plain `.` would make nix try git+file and fail.
nix develop path:. -c clab version

# clab deploys need root. The host solves this with a setuid-root
# /usr/bin/containerlab; mirror that here: the nix store is read-only, so
# install a setuid-root COPY of the pinned flake binary into /usr/local/bin.
# The flake's `clab` wrapper prefers it on PATH (see flake.nix).
# (tail: the shellHook banner prints to stdout before the command output)
CLAB_BIN="$(nix develop path:. -c sh -c 'command -v containerlab' | tail -n1)"
sudo install -o root -g root -m 4755 "$CLAB_BIN" /usr/local/bin/containerlab
sudo ln -sf /usr/local/bin/containerlab /usr/local/bin/clab
nix develop path:. -c clab version | grep 'version:'

echo ">>> dev container ready: nix develop path:., then cd lab && just up"
