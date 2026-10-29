# ot_anom_dect

Simulate Modbus TCP traffic in a small containerlab network, capture it,
and (next step) detect anomalies with k-means.

## Start in 3 commands

```sh
direnv allow           # activate the nix dev shell (once, at this root)
cd lab && just up      # 2 containers: Modbus server + master (first run ~1 min)
just hello             # hello world: register writes + reads across the network
just down              # tear down
```

## What each file is

| Path | What it is |
|---|---|
| `lab/justfile` | the 3 lab commands: `up`, `hello`, `down` |
| `lab/clab-ot-anom.clab.yml` | the topology: `modbus-server` (Modbus TCP slave on :502, holds the register map) + `modbus-master` (polls and writes); also read by the VS Code Containerlab extension |
| `lab/modbus/` | the two pymodbus scripts bind-mounted into the nodes: `server.py` (register map + request log) and `master.py` (write + poll exchange) |
| `docs/primer-modbus-kmeans.md` | **read this**: Modbus TCP + k-means onboarding for this project |
| `flake.nix` + `flake.lock` + `.envrc` | reproducible dev shell (nix + direnv) |

There is no Dockerfile: the nodes are stock `debian:bookworm-slim` images and
`just up` installs `python3-pymodbus` inside them (fine while the lab is this
small; bake an image when the lab grows).

Goal: a solid, well-evaluated k-means (and beyond) detector for Modbus TCP
traffic.
