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
            ];
            shellHook = ''
              echo "ot-anom dev shell: python312 (+pandas/sklearn/pymodbus), just, tshark"
              echo "lab: cd lab && just --list   (containerlab + docker; container names clab-ot-anom-*)"
            '';
          };
        });
    };
}
