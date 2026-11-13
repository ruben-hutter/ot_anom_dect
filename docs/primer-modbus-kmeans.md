# Primer: Modbus TCP and k-means for OT traffic anomaly detection

*Purpose: get you from "I don't know Modbus at all" to "I know which fields to featurize and how
k-means applies to traffic patterns". Spec references: MODBUS Application Protocol Specification
V1.1b3 (<https://www.modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf>) and *MODBUS
Messaging on TCP/IP Implementation Guide V1.0b* (<https://www.modbus.org/docs/Modbus_Messaging_Implementation_Guide_V1_0b.pdf>),
both from modbus.org.*

---

## 1. The project in one page

The approach:

1. **k-means clustering** → learn a baseline of "normal" OT network traffic
2. Deviations from the baseline = **anomalies / potential security incidents**
3. Optionally, an **LLM contextualizes** each incident (natural-language description +
   risk assessment) — a possible future layer, not the current focus

First iteration = a solid k-means baseline on Modbus TCP traffic; the improvement space (better
features, better detectors, better evaluation) is wide open — see §4.5.

The pipeline we are building:

```
containerlab lab                  monitor                  featurizer             detector
┌───────────────────┐   ┌──────────────────────┐   ┌────────────────────┐   ┌─────────────────┐
│ modbus-server     │──▶│ tcpdump/tshark → pcap│──▶│ 10-min sim windows │──▶│ k-means (fit on │
│ (slave, registers)│   │ (passive SPAN-style  │   │ → feature vectors  │   │ normal), score  │
│ modbus-master,... │   │  sniff) → JSONL      │   │ (see §3)           │   │ = dist to centro│
│ attacker          │   └──────────────────────┘   └────────────────────┘   └─────────────────┘
└───────────────────┘
```

---

## 2. Modbus TCP crash course

### 2.1 What it is

Modbus is the granddaddy of OT protocols: a 1979 register-exchange protocol for PLCs, still the
most widely deployed SCADA/OT protocol in the world. It is deliberately dumb — a **master/slave
(client/server) poll-response protocol over a flat register space**:

- The **master** (HMI, SCADA server, historian) initiates *every* exchange: it polls registers
  from, or writes registers to, a **slave** (a PLC, RTU, meter, drive...). Slaves never speak
  unprompted.
- All a slave is, from the network's point of view, is a table of registers plus a unit id.

Variants: Modbus RTU/ASCII (serial), and **Modbus TCP** (RFC-style encapsulation in TCP, the
de-facto standard port is **502**). The lab uses Modbus TCP because it's what flows across real
segmented OT networks (Purdue L2↔L1 traffic is very often Modbus TCP polls), and because it's
plain TCP you can capture and dissect anywhere. The original serial framing had no auth and no
encryption, and Modbus TCP carried both omissions into the IP world: **anyone who can reach
tcp/502 can read and write the process**. No handshakes, no secrets, no integrity — that is
exactly the attack surface this project targets.

### 2.2 The four register spaces (the whole data model)

| Space | Size | Access | Master ops (function codes) | Typical content |
|---|---|---|---|---|
| **Coils** (0xxxx) | 1 bit | read/write | 1 read, 5 write single, 15 write multiple | switches, commands, relays |
| **Discrete inputs** (1xxxx) | 1 bit | read-only | 2 read | alarms, limit switches |
| **Holding registers** (3xxxx) | 16 bit | read/write | 3 read, 6 write single, 16 write multiple | setpoints, process values, config |
| **Input registers** (4xxxx) | 16 bit | read-only | 4 read | sensor readings, counters |

Every request names: **unit id** (which slave; on TCP it's 1–247, often just 1 — on TCP it
mostly survives as a sub-device/gateway routing hint), **function code** (the operation),
**starting address** and **quantity**. That's the entire addressing scheme — 16-bit addresses,
no namespaces, no object model.

### 2.3 Function codes you'll actually see

| FC | Meaning | Direction | Why it's interesting for detection |
|----|---------|-----------|------------------------------------|
| 1  | read coils | master ← slave | polling profile |
| 2  | read discrete inputs | master ← slave | alarm-polling profile |
| 3  | read holding registers | master ← slave | **the** workhorse poll; per-address traffic profiles |
| 4  | read input registers | master ← slave | sensor-polling profile |
| 5  | write single coil | master → slave | **commands**: open a breaker, start a pump |
| 6  | write single register | master → slave | **setpoint writes** — rare in steady state, sensitive |
| 15 | write multiple coils | master → slave | bulk commands |
| 16 | write multiple registers | master → slave | bulk writes: config download, recipe change |
| 8  | diagnostics | master ↔ slave | mostly benign, but interesting |
| 43/14 | read device identification | master ← slave | classic **recon/enum** step |

Responses echo the function code on success, or answer with `fc | 0x80` plus a 1-byte
**exception code** (01 illegal function, 02 illegal data address, 03 illegal data value,
04 server device failure, 06 server device busy...). A storm of exceptions = someone probing
addresses or functions the slave doesn't support — loud in the pcap, cheap to count.

### 2.4 The wire format — the fields you can cluster on

Modbus TCP PDU = 1 byte function code + data (≤ 253 B). Modbus TCP ADU = PDU wrapped in the
7-byte **MBAP header**:

```
transaction id (2 B) | protocol id (2 B, =0) | length (2 B) | unit id (1 B) | PDU
```

- **transaction id** — set by the master, echoed by the slave; matches request↔response (and
  lets one TCP connection multiplex concurrent polls). Orphaned transaction ids = suspicious.
- **unit id** — which slave (or sub-device behind a gateway) is addressed; new/swept unit ids
  are interesting.
- **function code** — the single most discriminative byte in the protocol: read vs write mix
  per window is your baseline's backbone.
- **starting address + quantity** — *where* in the register map the traffic points. Per-address
  counts and address entropy are your "who is touching what" features.
- **exception responses** (`fc|0x80`, exception code) — probing shows up here.

Everything is plaintext: from a passive capture you get all of the above *plus* TCP-level
features (connections, inter-arrival times, segment sizes) for free. TCP port 502 is the
convention (IANA-registered); the lab may use it or not — the framing is identical either way.

### 2.5 Traffic shape: poll cycles are the "normal"

Real Modbus traffic is boring by design, and boring is learnable:

- The master **polls cyclically** (e.g. read FC3 of 10 registers every second) — near-perfect
  inter-arrival regularity. This is the bulk of all traffic.
- **Writes are rare**: operator setpoint changes, occasional recipes/config. Tens of writes per
  day, vs millions of reads (see the measured numbers in
  [research-ot-traffic-profiles.md](research-ot-traffic-profiles.md)).
- Traffic is a small set of **repeating request/response pairs** — a handful of (unit id, fc,
  address, count) tuples cycling forever.

Consequence: an attacker who starts issuing writes, sweeping addresses, or polling at odd
intervals is *structurally* different from the baseline — this is why a simple per-window
feature model works at all.

### 2.6 Tools you'll touch

| Tool                 | Use                                                       |
|----------------------|-----------------------------------------------------------|
| Python `pymodbus`    | pure-python master/slave — powers the lab, and later traffic replay/injection |
| `mbpoll` / `mbtget`  | one-off CLI register reads/writes from shell              |
| `tshark`/Wireshark   | dissects Modbus on tcp/502 natively (`-Y mbtcp`); pcap workbench |
| `tcpdump`            | passive capture on the sensor node                        |

### 2.7 The other Modbus variants — same protocol, different pipe

Modbus is **one application protocol** (the PDU: function code + data, ≤253 B) wearing several
ADU framings. The variants differ only in the wrapping:

| Variant   | ADU framing                                                          | Transport            | Status |
|-----------|----------------------------------------------------------------------|----------------------|--------|
| **RTU**   | slave addr (1 B) + PDU + CRC-16 (2 B); frames delimited by 3.5-char silence | EIA-232/485 serial | ubiquitous at field level |
| **ASCII** | same PDU, hex-encoded, LRC checksum                                  | serial               | effectively dead |
| **TCP**   | MBAP header + PDU (§2.4)                                             | tcp/502, Ethernet    | **this lab** — the inter-level standard |
| **Modbus+** | token ring over HDLC — its own protocol below the PDU              | proprietary Schneider | effectively dead |
| "other"   | vendor flavors (RTU-over-UDP, Modbus/TLS, Enron/Daniel dialects)     | various              | niche |

**What transfers between variants.** Everything that lives in the PDU: the whole §3 feature
backbone (fc mix, read:write ratio, per-address counts/entropy, unit-id sweeps, exception
storms, poll-dictionary size) applies to RTU unchanged. TCP-only features that don't exist on
serial: transaction ids / orphans, TCP connection behavior. Serial-only features TCP can't
teach: CRC-error counts (garbled frames), silence-gap framing — and "new master" means a new
speaking slave address, not a new IP.

**Real deployments close most of the gap for you**: serial slaves usually sit behind an
RTU→TCP gateway, so the tap point on segmented networks sees Modbus TCP with unit ids
multiplexing the serial side (§2.2). Modeling the field as distinct unit ids — as this lab
does — is faithful to that reality. TCP-first is therefore not a shortcut but the realistic
vantage point.

**Why not simulate other buses in the lab**: containerlab is netns + Ethernet/IP; a serial bus
isn't a network in that model, and there is no `tcpdump` on a PTY. Emulating RTU (pymodbus
serial framer over socat PTY pairs) is possible, but capture degrades to app-layer JSONL
logging by an interposed shim — losing the passive-pcap sensor model the project is built on.
ASCII and Modbus+ are museum pieces; skip them. If serial coverage is ever genuinely needed,
the cheap honest route is hardware: a ~€10 RS-485 USB adapter pair plus an interposing
sniffer node — outside containerlab. The higher-value extension is *different protocols*
(S7comm, DNP3, IEC 104, EtherNet/IP): the methodology (windows → features → one-class k-means,
§3–4) transfers, only the dissector/featurizer changes.

**Design note**: keep **PDU features** (variant-independent) separated from **framing/transport
features** (variant-specific) in the featurizer, so adding a serial feeder later is a new
extractor, not a rewrite.

---

## 3. From raw messages to features (what to cluster on)

k-means needs **fixed-length numeric vectors**. You don't cluster messages; you cluster
**time windows**. Slice the stream into windows (e.g. 10 minutes of simulated time), count
things per window:

**Counts & rates**
- requests/window, split by function code (reads FC1–4 vs writes FC5/6/15/16)
- per-address counts (holding-register polls, coil writes, ...), bytes/window total
- requests per unit id; number of unit ids seen
- **new masters this window** (first-seen source IP / first-seen transaction patterns)

**Structure**
- **read:write ratio** (writes are a rounding error in steady state — a write burst *is* the alarm)
- distinct register addresses touched; **register-address entropy** ("scanning" sweeps the map and spikes it)
- address-range span (min/max address touched vs baseline envelope)
- **exception-response share** (`fc|0x80` storms = probing)
- distinct (unit id, fc, address, count) tuples — the "poll dictionary" size; new tuples = new behavior
- avg / max ADU size (bulk writes stand out); transaction ids without responses (orphans)

**Timing**
- inter-arrival mean & std (**poll-interval regularity** — machine polls have tiny std; human-driven
  or malicious traffic on the same wire doesn't)
- **hour of day encoded as sin/cos** (see §4) — your 08:00–17:00 vs 03:00 scenario lives here

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

Your example, concretely: normal days build a "business hours" cluster — high poll rates,
FC3 reads over the usual address ranges, hour∈[8,17] — and a quiet "night" cluster — cyclic
polls only. At 03:00 an unknown IP writes to register ranges it never touched and sweeps new
addresses: hour≈3, write-heavy fc mix, address entropy through the roof, new source → this
window is far from *every* centroid → alarm.

### 4.3 Preprocessing that actually matters

- **Scale every feature** (`StandardScaler`): k-means sums squared distances, so unscaled
  "request count ~5000" would drown "unit ids seen ~2". Fit scaler on training data only.
- **Hour of day is circular**: 23:50 and 00:10 are 20 min apart but numerically 23.8 apart in
  hours. Encode `hour_sin = sin(2πh/24)`, `hour_cos = cos(2πh/24)`.
- **Categorical explosion**: don't one-hot thousands of register addresses. Count per address
  (top-N + "other"), or aggregate per register space / address family. (k-means on raw one-hot
  is geometrically meaningless anyway — distances between sparse binary vectors are all ~equal.)
- Optional: log-transform heavy-tailed counts (`np.log1p`) before scaling.

### 4.4 Choosing k

- **Elbow**: plot inertia vs k, look for the bend (weak but universal).
- **Silhouette**: mean separation quality, higher = better; usually you'll see k≈4–10 for a
  handful of services with day/night profiles.
- For *anomaly detection*, slightly-too-small k is fine (cruder model → stricter "normal");
  k-means is a *baseline*, not the endgame — don't over-tune it.

### 4.5 Known weaknesses = your improvement menu

| k-means weakness                                   | Better alternative                                        |
|----------------------------------------------------|----------------------------------------------------------|
| spherical, equal-variance clusters                  | **GMM** (soft boundaries, per-cluster covariance)        |
| centroids dragged by outliers                       | **Isolation Forest**, **LOF** (purpose-built detectors)  |
| one global "normal"                                 | **per-slave/per-master baselines** (feature grouping)    |
| no temporal structure (windows treated i.i.d.)      | rates & trend features, HMM/LSTM on window sequences     |
| hard count threshold on distances                   | calibrated thresholds, FPR budget per hour-of-day        |
| no notion of "gradually drifting normal"            | rolling retrain / drift detection                        |

You don't need all of these — pick 2–3, evaluate properly (§4.6). A downstream LLM
contextualization layer (natural-language incident reports) would be *complementary*:
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

See the repo README for the hands-on part. Mapping of concepts:

| Lab piece                          | Real-world analogue                     | Output you cluster on        |
|------------------------------------|-----------------------------------------|------------------------------|
| `modbus-server` node (pymodbus slave + register map) | the OT device / PLC / RTU on the network | pcap + request log           |
| `modbus-master` node (legit polling) | legitimate operators/SCADA/HMI      | the "normal" training data   |
| `attacker` node                    | compromised host on the OT network      | labeled anomaly injections   |
| `tcpdump`/`tshark` passive capture  | your passive SPAN sensor               | pcap, per-message JSONL      |
| simulated clock (`data/clock.json`)| — (lets you compress days into minutes) | `sim_hour` column            |

Suggested order of work once the lab runs:

1. Collect ~10 simulated "normal days" (legit polling running, attacker off).
2. Featurize into windows (small pandas script — next iteration of this repo).
3. Fit k-means + threshold, eyeball clusters (hours vs counts scatter).
4. Run attack scenarios → see alarms fire → compute precision/recall.
5. Only now start improving features/detector.
