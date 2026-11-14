# ot_anom_dect

Simulate Modbus TCP traffic in a small containerlab network, capture it,
and detect anomalies with one-class k-means over 60 s traffic windows.
Current results: [`reports/results-kmeans.md`](reports/results-kmeans.md).

## Start in 3 commands

```sh
direnv allow           # activate the nix dev shell (once, at this root)
cd lab && just up      # 2 containers: PLC + HMI (first run ~1 min)
just run               # HMI polls the PLC: watch the process value move
just down              # tear down
```

`just hello` is a one-shot smoke test, `just sp 300` simulates an operator
setpoint change, and `just capture` produces a pcap of the poll traffic in
`lab/data/`. The **offline data pipeline needs no lab at all**:
`cd lab && just simulate`, `just featurize …`, `just sim-all` generate
lab-like traffic (normal + 5 anomaly scenarios) and window features.
Details: `lab/README.md`.

## What each file is

| Path | What it is |
|---|---|
| `lab/justfile` | lab commands: `up`, `hello`, `run`, `sp`, `capture`, `down` |
| `lab/clab-ot-anom.clab.yml` | the topology: `modbus-server` (Modbus TCP PLC on :502, register map + 1 Hz process simulation) + `modbus-master` (HMI poller / operator writes); also read by the VS Code Containerlab extension |
| `lab/modbus/` | the pymodbus scripts bind-mounted into the nodes: `server.py` (PLC + process sim + request log), `master.py` (HMI/SCADA fixed-period poller), `op.py` (operator setpoint write) |
| `lab/simulate.py` | **offline** traffic simulator → `events.jsonl` (pinned contract): normal + 5 anomaly scenarios, deterministic per `--seed` |
| `lab/featurize.py` | **offline** featurizer → `windows.csv` (pinned schema) from `events.jsonl` and/or pcaps (host-side tshark) |
| `lab/detect_kmeans.py` | **offline** one-class k-means detector → `alerts CSV` (`window_id,score,alert`); train on a normal reference (`--train-file`) or on the head of the input; `--selftest` included |
| `reports/` | results report with plots (`results-kmeans.md`), detector method doc (`detect-kmeans.md`), plot script |
| `docs/primer-modbus-kmeans.md` | **read this**: Modbus TCP + k-means onboarding for this project |
| `flake.nix` + `flake.lock` + `.envrc` | reproducible dev shell (nix + direnv) |

There is no Dockerfile: the nodes are stock `debian:bookworm-slim` images and
`just up` installs `python3-pymodbus` inside them (fine while the lab is this
small; bake an image when the lab grows).

## Results

The detector flags **99–100% of windows** in all five injected scenarios
(poll storm, write flood, off-schedule polling, rogue master, exception
storm) at a **1.7% alert rate on normal** traffic; features extracted from a
real 120 s lab capture match the simulator's normal magnitudes. Tables,
plots and one-command reproduction: [`reports/results-kmeans.md`](reports/results-kmeans.md).

## Optional: dev container (docker-in-docker)

`.devcontainer/` defines an **opt-in** VS Code dev container that runs the lab
against a *nested* docker-in-docker daemon — the `clab-ot-anom-*` nodes never
appear on the host's docker. Nothing about the host workflow above changes;
both coexist.

- Open the repo in VS Code → **Reopen in Container**. The first open is slow
  (nix is installed and the flake store is downloaded once, ~a few minutes);
  later opens are fast.
- Inside, the toolchain comes from the same flake: `nix develop path:.`
  (note the `path:.` — the repo's wtree `.git` points at an absolute host
  path that doesn't exist inside the container, and `path:` skips git
  detection), then `cd lab && just up / hello / down` work identically
  against the nested daemon.
- `clab` deploys need root, so `post-create.sh` installs a setuid-root copy of
  the flake-pinned binary in `/usr/local/bin` (same arrangement as the host's
  `/usr/bin/containerlab`).
- Caveats: the VS Code Containerlab extension may not find `clab` inside the
  container (extension shells don't inherit the nix PATH); do git work from
  the host — the container sees the repo as a plain folder.

Goal: a solid, well-evaluated k-means (and beyond) detector for Modbus TCP
traffic.
