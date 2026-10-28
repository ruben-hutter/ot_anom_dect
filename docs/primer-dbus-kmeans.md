# Primer: D-Bus and k-means for OT event-stream anomaly detection

*Purpose: get you from "I don't know D-Bus at all" to "I know which fields to featurize and how
k-means applies to traffic patterns". Spec reference: https://dbus.freedesktop.org/doc/dbus-specification.html*

---

## 1. The project in one page

The Basel bachelor thesis ("LLM-based Security-Compliance in Event Streams", Valentin Neher,
supervised by Marco Vogt) does:

1. **k-means clustering** → learn a baseline of "normal" OT network traffic
2. Deviations from the baseline = **anomalies / potential security incidents**
3. An **LLM contextualizes** each incident (natural-language description + risk assessment)

Your job: **make that work better**. First iteration = solid k-means baseline on D-Bus traffic;
the improvement space (better features, better detectors, better evaluation) is wide open —
see §5.

The pipeline we are building:

```
containerlab lab                monitor                  featurizer             detector
┌─────────────────┐   ┌────────────────────┐   ┌────────────────────┐   ┌─────────────────┐
│ bus-host (OT dev)│──▶│ dbus-monitor→JSONL │──▶│ 10-min sim windows │──▶│ k-means (fit on │
│ client-a, -b     │   │ tcpdump → pcap     │   │ → feature vectors  │   │ normal), score  │
│ attacker         │   └────────────────────┘   │ (see §3)           │   │ = dist to centro│
└─────────────────┘                            └────────────────────┘   └─────────────────┘
```

---

## 2. D-Bus crash course

### 2.1 What it is

D-Bus is Linux's standard **IPC (inter-process communication) message bus**: a small central
daemon that applications connect to and exchange typed messages through. It comes in two
flavors you'll find on any desktop/server Linux:

- **system bus** — system-wide services: systemd (logind, timedated, resolved...), NetworkManager, udisks, timer/UPS daemons
- **session bus** — one per logged-in user, used by desktop apps

It is also ubiquitous in **embedded and OT-ish devices** (gateways, NUC-class industrial PCs,
set-top boxes, routers with web UIs) because systemd brings it along for free. That's why
"D-Bus as the protocol on the network" is a reasonable OT scenario: plenty of real devices
expose D-Bus, and (as we'll do in the lab) it *can* be run over TCP — unencrypted,
weakly authenticated. That is exactly the attack surface the thesis is about.

### 2.2 Architecture in 30 seconds

- Every client connects to the **bus daemon** (one TCP/unix socket), authenticates, and calls
  `Hello()` → gets a **unique name** like `:1.42` (starts with `:`, never reused).
- Services **claim well-known names** via `RequestName`, e.g. `com.otanom.device` — the D-Bus
  equivalent of a service registering itself in a lookup table.
- A service exposes **objects** (paths like `/com/otanom/device/sensors`), each with
  **interfaces** (namespaced method/signal/property sets like `com.otanom.device.sensors`).
- **Method calls** are request/response (call → `METHOD_RETURN` or `ERROR` reply).
  **Signals** are fire-and-forget broadcasts; clients opt in with match rules (`AddMatch`).
- The daemon **routes** messages: destination-based for calls, match-rule-based for signals,
  and stamps every message with the sender's unique name (clients can't forge `SENDER`).

### 2.3 The four message types (spec §Message type)

| Type            | Value | Meaning                                   | Reply?           |
|-----------------|-------|-------------------------------------------|------------------|
| `METHOD_CALL`   | 1     | "invoke this method on that object"       | expects reply    |
| `METHOD_RETURN` | 2     | successful reply                          | —                |
| `ERROR`         | 3     | error reply (has `ERROR_NAME`)            | —                |
| `SIGNAL`        | 4     | broadcast emission, no destination needed | —                |

### 2.4 Message anatomy — the fields you can cluster on

Every message = fixed header + array of **header fields** + body. From the spec, the header
fields that exist (field code → name):

| Code | Field         | Type          | Present on        | Why it's interesting for detection                      |
|------|---------------|---------------|-------------------|---------------------------------------------------------|
| 1    | `PATH`        | OBJECT_PATH   | calls, signals    | weird paths = probing/scanning                          |
| 2    | `INTERFACE`   | STRING        | signals (req), calls (opt) | **the** discriminator: per-interface traffic profiles |
| 3    | `MEMBER`      | STRING        | calls, signals    | method/signal name — sensitive ops (`Reset`, `Upload`)  |
| 4    | `ERROR_NAME`  | STRING        | errors            | error storms, access-denied spam                        |
| 5    | `REPLY_SERIAL`| UINT32        | replies           | correlates call↔reply; orphans = suspicious             |
| 6    | `DESTINATION` | STRING        | bus-routed msgs   | who is being talked to; new destinations                |
| 7    | `SENDER`      | STRING        | stamped by bus    | **unique name of source** → per-client baselines        |
| 8    | `SIGNATURE`   | SIGNATURE     | if body non-empty | payload shape; changes = new/abnormal behavior          |
| 9    | `UNIX_FDS`    | UINT32        | fd passing        | unusual off-box (mostly unix-socket only)               |

Plus the fixed header: endianness byte, **message type**, flags (`NO_REPLY_EXPECTED`,
`NO_AUTO_START`, ...), protocol version, body length, serial. The **body length + TCP segment
sizes** give you payload-volume features for free from the pcap.

### 2.5 Connection lifecycle events (also visible at the bus)

These are bus-level "meta messages" — often the loudest attack signals:

- `Hello()` — **every new connection** → a new client appeared (new IP if TCP!)
- `RequestName` / `ReleaseName` — service claiming/leaving names; **name squatting/spoofing** shows up here (`NameOwnerChanged`, `NameAcquired`)
- `AddMatch` — who subscribes to whom (recon: "let me listen to everything")
- SASL handshake before everything: `AUTH EXTERNAL <hex uid>` … `BEGIN` — visible in the pcap
  (over TCP, `EXTERNAL` degenerates to "client declares a uid, bus trusts it" → weak auth,
  one reason TCP D-Bus is considered unsafe)

### 2.6 Tools you'll touch

| Tool                 | Use                                                       |
|----------------------|-----------------------------------------------------------|
| `busctl`             | inspect a bus live: `busctl --address=... tree/list`       |
| `dbus-monitor`       | dump every message (`--profile` = one line per msg — our monitor uses this) |
| `dbus-send` / `gdbus`| one-off calls from shell                                   |
| Python `jeepney`     | pure-python client lib — a good choice for the detector later (its unix-only limitation needs a small TCP workaround) |
| Python `dbus-next`   | asyncio alternative, fine too                              |

---

## 3. From raw messages to features (what to cluster on)

k-means needs **fixed-length numeric vectors**. You don't cluster messages; you cluster
**time windows**. Slice the stream into windows (e.g. 10 minutes of simulated time), count
things per window:

**Counts & rates**
- messages/window, split by message type (calls / returns / errors / signals)
- per-interface counts (`com.otanom.device.sensors`, `*.admin`, …), per-member counts
- messages per unique `SENDER`; number of unique senders
- **new clients this window** (first-seen `Hello` / first-seen sender or source IP)

**Structure**
- call:return:signal:error ratio (reply storms / error storms break it)
- share of `NO_REPLY_EXPECTED` calls (flooding tools skip replies)
- distinct (`INTERFACE`,`MEMBER`) pairs — interface entropy ("scanning" spikes it)
- avg / max body length (exfil = big payloads); bytes/window total

**Timing**
- inter-arrival mean & std (machine-like floods have tiny std)
- **hour of day encoded as sin/cos** (see §4) — your 08:00–17:00 vs 03:00 scenario lives here

**Bus-meta**
- `RequestName` count, `NameOwnerChanged` count, `AddMatch` count per window

Rule of thumb: start with ~15–25 features. Every feature should earn its place by catching
something in your threat model (see lab README scenario→feature mapping table).

---

## 4. k-means refresher, applied to traffic

### 4.1 The algorithm (Lloyd's, with k-means++ init)

1. Pick **k** centroids (k-means++: spread them out probabilistically).
2. Assign every point to its nearest centroid (Euclidean distance).
3. Move each centroid to the mean of its points.
4. Repeat 2–3 until stable. You get: cluster labels + inertia (sum of squared distances).

In sklearn: `KMeans(n_clusters=k, n_init=10, random_state=0).fit(X)`.

### 4.2 The setup for anomaly detection (this is the key trick)

**Train on normal data only.** Then k-means stops being a classifier and becomes a
**one-class model**: normal traffic forms tight clusters; anything far from *all* centroids
is anomalous.

```
anomaly_score(window) = min_j || x_window − centroid_j ||      # distance to nearest centroid
threshold             = 99th percentile of scores on the training (normal) set
alarm                 ⇔  anomaly_score > threshold
```

Your example, concretely: normal days build a "business hours" cluster — high call rates,
sensors/HMI interfaces, hour∈[8,17] — and a quiet "night" cluster — heartbeats only. At 03:00
an unknown IP floods an admin interface: hour≈3, huge message count, unseen interface, new
sender → this window is far from *every* centroid → alarm.

### 4.3 Preprocessing that actually matters

- **Scale every feature** (`StandardScaler`): k-means sums squared distances, so unscaled
  "message count ~5000" would drown "unique senders ~3". Fit scaler on training data only.
- **Hour of day is circular**: 23:50 and 00:10 are 20 min apart but numerically 23.8 apart in
  hours. Encode `hour_sin = sin(2πh/24)`, `hour_cos = cos(2πh/24)`.
- **Categorical explosion**: don't one-hot 500 interfaces. Count *per interface* (top-N +
  "other"), or aggregate per *interface family*. (k-means on raw one-hot is geometrically
  meaningless anyway — distances between sparse binary vectors are all ~equal.)
- Optional: log-transform heavy-tailed counts (`np.log1p`) before scaling.

### 4.4 Choosing k

- **Elbow**: plot inertia vs k, look for the bend (weak but universal).
- **Silhouette**: mean separation quality, higher = better; usually you'll see k≈4–10 for a
  handful of services with day/night profiles.
- For *anomaly detection*, slightly-too-small k is fine (cruder model → stricter "normal");
  k-means is a *baseline*, not the endgame — don't over-tune it.

### 4.5 Known weaknesses = your improvement menu over the thesis

| k-means weakness                                   | Better alternative (project "improvement" material)      |
|----------------------------------------------------|----------------------------------------------------------|
| spherical, equal-variance clusters                  | **GMM** (soft boundaries, per-cluster covariance)        |
| centroids dragged by outliers                       | **Isolation Forest**, **LOF** (purpose-built detectors)  |
| one global "normal"                                 | **per-service/per-sender baselines** (feature grouping)  |
| no temporal structure (windows treated i.i.d.)      | rates & trend features, HMM/LSTM on window sequences     |
| hard count threshold on distances                   | calibrated thresholds, FPR budget per hour-of-day        |
| no notion of "gradually drifting normal"            | rolling retrain / drift detection                        |

You don't need all of these — pick 2–3, evaluate properly (§4.6), and you already have a
defensible "made it better" story. The thesis's LLM contextualization layer is *complementary*:
your detector feeds it better incidents.

### 4.6 Evaluating honestly

Inject **labeled** attacks into hours of normal traffic (the lab does exactly this), then:

- precision / recall / F1 per attack type at a fixed threshold
- false alarms per simulated day (operators ignore noisy systems)
- detection *delay* (which window flags it first)
- compare k-means vs your improved detector on identical features (that comparison *is* your
  contribution)

---

## 5. The lab — how this becomes concrete

See `lab/README.md` for the hands-on part. Mapping of concepts:

| Lab piece                          | Real-world analogue                     | Output you cluster on        |
|------------------------------------|-----------------------------------------|------------------------------|
| `bus-host` node (dbus-daemon+OT service) | the OT device / bus on the network | pcap + monitor JSONL         |
| `client` node (legit traffic)       | legitimate operators/SCADA/HMI      | the "normal" training data   |
| `attacker` node                    | compromised host on the OT network      | labeled anomaly injections   |
| `dbus-monitor` + `tcpdump` on bus-host | your passive network sensor        | JSONL message log, pcap      |
| simulated clock (`data/clock.json`)| — (lets you compress days into minutes) | `sim_hour` column            |

Suggested order of work once the lab runs:

1. Collect ~10 simulated "normal days" (`make normal` running, attacker off).
2. Featurize into windows (small pandas script — next iteration of this repo).
3. Fit k-means + threshold, eyeball clusters (hours vs counts scatter).
4. Run attack scenarios → see alarms fire → compute precision/recall.
5. Only now start improving features/detector.
