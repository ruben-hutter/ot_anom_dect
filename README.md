# ot_anom_dect

Simulate D-Bus-over-TCP traffic in a small containerlab network, capture it,
and (next step) detect anomalies with k-means — the detection baseline for the
Basel thesis "LLM-based Security-Compliance in Event Streams".

## Start in 3 commands

```sh
direnv allow           # activate the nix dev shell (once, at this root)
cd lab && just up      # 2 containers: D-Bus bus + client (first run ~1 min)
just hello             # hello world: method call + signal across the network
just down              # tear down
```

## What each file is

| Path | What it is |
|---|---|
| `lab/justfile` | the 3 lab commands: `up`, `hello`, `down` |
| `lab/clab-ot-anom.clab.yml` | the topology: `bus-host` (D-Bus daemon on TCP :55556) + `client`; also read by the VS Code Containerlab extension |
| `lab/dbus/system.conf` | the one extra config file that's genuinely required: the stock dbus config only listens on unix sockets and authenticates via kernel credentials — neither exists across containers, so this switches the bus to TCP + no-auth |
| `docs/primer-dbus-kmeans.md` | **read this**: D-Bus + k-means onboarding for this project |
| `flake.nix` + `flake.lock` + `.envrc` | reproducible dev shell (nix + direnv) |

There is no Dockerfile: the nodes are stock `debian:bookworm-slim` images and
`just up` installs dbus inside them (fine while the lab is this small; bake an
image when the lab grows).

Background: the thesis being improved studies k-means baselining of OT traffic
with an LLM contextualizing incidents; our angle is a solid, well-evaluated
k-means (and beyond) detector for D-Bus event streams.
