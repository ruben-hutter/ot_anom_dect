{
  # Reproducible dev environment for the ot-anom project (Modbus TCP OT anomaly
  # detection). Enter with direnv (`direnv allow`) or `nix develop`.
  #
  # Design notes:
  # - The analysis language is not decided yet; the pinned python stack below
  #   (numpy/pandas/scikit-learn/matplotlib) is the default starting point.
  #   pymodbus is included so the host can also act as a Modbus master against
  #   the lab (and later replay/inject traffic). Add packages here instead of
  #   pip-installing ad hoc.
  # - just orchestrates the lab (lab/justfile).
  # - Version pins travel with flake.lock (committed) => reproducible.

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: builtins.listToAttrs
        (map (s: { name = s; value = f s; }) systems);
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          # nixpkgs installs the binary as `containerlab`; the lab invokes
          # `clab`. clab deploys need root: hosts typically provide a
          # setuid-root binary (host /usr/bin/containerlab; the dev container
          # gets one in /usr/local/bin via .devcontainer/post-create.sh).
          # Prefer a setuid copy when one is on PATH — the nix store is
          # read-only, so the flake's own binary cannot carry the bit — else
          # fall back to the pinned nixpkgs binary.
          clab = pkgs.writeShellScriptBin "clab" ''
            while IFS= read -r b; do
              [ -u "$b" ] && exec "$b" "$@"
            done < <(type -aP containerlab)
            exec ${pkgs.containerlab}/bin/containerlab "$@"
          '';
          py = pkgs.python312.withPackages (ps: with ps; [
            numpy
            pandas
            scikit-learn
            matplotlib
            pymodbus             # Modbus TCP client for poking/replaying lab traffic
            # later, when needed: ps.jupyterlab ps.seaborn ps.plotly
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              py
              pkgs.just             # lab orchestration (lab/justfile)
              pkgs.wireshark-cli    # tshark, pcap inspection from the host
              pkgs.containerlab     # lab deploy; flake-pinned so the dev container gets the same clab
              clab                  # ... as `clab`, setuid-dispatching (see above)
            ];
            shellHook = ''
              echo "ot-anom dev shell: python312 (+pandas/sklearn/pymodbus), just, tshark, clab"
              echo "lab: cd lab && just --list   (containerlab + docker; container names clab-ot-anom-*)"
            '';
          };
        });
    };
}
