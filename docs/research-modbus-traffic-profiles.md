# Research: What realistic Modbus/TCP traffic looks like (for the k-means baseline)

*Purpose: answer "what does realistic Modbus/TCP traffic look like — frame anatomy, function codes,
measured characteristics from real deployments — so the lab can simulate Modbus that a k-means
anomaly-detection baseline can honestly learn from, and which Modbus fields are featurizable?"
Replaces the Modbus-relevant parts of [research-ot-traffic-profiles.md](research-ot-traffic-profiles.md):
that doc's D-Bus assumption was wrong (the OT protocol of interest is Modbus/TCP, not D-Bus); its
OT-context sections stay valid and are adapted in §1. k-means mechanics and windowing:
[primer-dbus-kmeans.md](primer-dbus-kmeans.md) (§3–4; the pipeline is protocol-agnostic). Method:
primary sources first (modbus.org specs, peer-reviewed measurement studies); every claim carries a
citation `[n]` into [Sources](#sources), with spec section where possible. The official Modbus spec
PDFs (Application Protocol V1.1b3, TCP Messaging Guide V1.0b) are vendored in-repo under `docs/specs/`
as ground truth. Bot-blocked canonical URLs
are paired with Wayback mirrors. Secondary sources are marked **(secondary)**.*

---

## 1. OT context (protocol-agnostic, trimmed)

The standard segmentation model is the **Purdue model** (levels 0–4/5), which NIST SP 800-82r3
adopts alongside ISA-95/IEC 62264 levels, recommending DMZ-mediated crossing: firewall policy
should prevent Level 4 devices from directly reaching Levels 2/1/0 [1, §5.2.3.1, Fig. 16].
Supervisory and lower-level control loops "operate continuously ... with cycle times ranging in
the order of milliseconds to minutes" [1, §2.3]; many OT processes run 24/7 with outages scheduled
days–weeks ahead [1, §2.4]. Measured protocol placement (six real power-grid networks): IEC 104
between control rooms and plants/substations, ICCP between control centers, GOOSE IED-to-IED in
substations, C37.118 for PMUs, and **Modbus/TCP monitoring a consumer (campus) site** [2, Tables 1–2].

| Canonical OT traffic category | Typical interval / rate | Size | Direction | Evidence |
|---|---|---|---|---|
| Cyclic polling (master ↔ field) | ms–minutes per scan [1, §2.3]; campus Modbus had the highest packet rate of all studied networks (IAT 1–2 orders below other protocols) [2, §5.2.2] | small; Modbus ADU ≤ 260 B [5, §3.1.2] | ~50 % each way (request/response doubles volume) [2, Fig. 7] | [1, 2, 5] |
| Event / report-by-exception | real RTU: 0.6–19 spontaneous IEC-104 events/h (30-day captures) [3, Table 1]; deadband/change-triggered [3, §Data] | small (36 B typical IEC-104 payload) [2, Fig. 10] | field → master | [2, 3] |
| Alarms & sequence-of-events | rare: 4.4 % of ASDUs in a distribution network, <0.001 % in a bulk network [2, Tables 4–5] | small | field → master | [2] |
| Time sync / heartbeats | IEC-104 TESTFR at 20 s idle; **Modbus/TCP has no protocol-level heartbeat** (keepalive is TCP's, default off) [14, §3.8.4] | tiny | mixed | [2, 14] |
| Config / program / firmware | rare, bursty, operator-initiated (L3) [1, §2.3]; no public measured rate found | large (whole files) | master/L3 → field | [1] |
| Operator / HMI reads & writes | steady-state writes are a rounding error: setpoints ≤0.85 % of ASDUs; **Modbus write (FC5) <0.001 %** of messages [2, Tables 4, 6] | small | master → field | [2] |

Measured characteristics that carry over (protocol-agnostic): ~75 % of packets in *all* studied
networks are <100 B [2, §5.2.2]; regularity and volume dominate over payload semantics; real event
rates are so low that flows under 0.3 events/h were excluded from analysis [3, Table 1]. **Emulation
caution:** of three characteristics observed in emulated/tested ICS traffic, two appeared in real
traffic "in a less regular way" and one not at all — emulations over-generate and over-regularize
(emulated 16.9–67 events/h vs 0.6–19/h on real RTUs) [3, Abstract/Conclusions]. Jitter everything.

---

## 2. Modbus/TCP frame anatomy

Modbus/TCP = the Modbus PDU embedded in TCP, with a 7-byte MBAP header; all ADUs go to registered
**TCP port 502** [5, §3.1.3]. **ADU = MBAP (7 B) + PDU (≤ 253 B) ⇒ max 260 B** [5, §3.1.2].
One ADU per TCP frame: "A TCP frame must transport only one MODBUS ADU" [5, §4.2.1, rule 6].

| Field | Bytes | Semantics (request → response) | Detection interest |
|---|---|---|---|
| Transaction ID | 2 | Set by the client to identify/pair the transaction; **the server copies it into the response** [5, §3.1.3] | pairing, gaps, replay, counter quirks (below) |
| Protocol ID | 2 | 0 = Modbus; "used for intra-system multiplexing" [5, §3.1.3] | non-zero ⇒ protocol anomaly |
| Length | 2 | Byte count of everything after this field (unit ID + PDU) [5, §3.1.3] | frame boundary + size distribution |
| Unit ID | 1 | Routing to a slave *behind a gateway* (serial slaves 1–247). Directly-connected TCP server: Unit ID is "useless"; **0xFF recommended, 0x00 also accepted**; server must echo it [5, §4.4.1.3] | unit-ID sweeps = recon through gateways |
| Function code | 1 | Action requested; normal response **echoes** it; exception response = FC + 0x80 [6, §4.1, §7] | per-FC profiles |
| Data | ≤252 | FC-specific: reference number (start address), bit/word count, values [6, §4.1] | register-address patterns |

Transaction-ID notes (including known implementation quirks):

- The spec assumes a unique TID per outstanding request; a response whose TID "doesn't refer to any
  MODBUS pending transaction" must be discarded [5, §4.4.1.3].
- Pipelining is legal: "Several MODBUS transactions can be activated simultaneously on the same TCP
  Connection" — then TIDs uniquely match requests/responses [5, §4.2.1, rule 4]. In measured
  production traffic, though, each HMI–PLC channel used a single long-lived TCP connection with
  strict sequential poll/response cycles [7, §3.2, §4].
- Implementation stacks differ: libmodbus keeps a per-connection counter, initialized to 0,
  incremented per request, wrapping 65535 → 0 [12, `src/modbus-tcp.c`] — a deterministic TID
  sequence. Deviations from the echo/pairing expectation (reused, jumping, or constant TIDs) are
  cheap-to-detect anomalies; Goldenberg & Wool encode the TID directly in their 33-bit traffic
  symbol [7, §3.2, §4.1].

## 3. Function-code taxonomy (from the application spec [6])

Valid codes are 1–255 with 128–255 reserved for exception responses and 0 invalid [6, §4.1];
"the Modbus standard defines the meaning of nineteen of the 127 possible function codes" [7, §3.1].

| Group | FCs | Notes |
|---|---|---|
| Bit access — read | 01 Read Coils, 02 Read Discrete Inputs | quantity ≤ 2000 (0x07D0) [6, §6.1–6.2] |
| Bit access — write | 05 Write Single Coil (value 0x0000/0xFF00), 15 Write Multiple Coils [6, §6.5, §6.11] | FC5 = the one write seen in measured campus traffic, at <0.001 % [2, Table 6] |
| Register access — read | 03 Read Holding Registers, 04 Read Input Registers | quantity ≤ 125 (0x007D) [6, §6.3–6.4] |
| Register access — write | 06 Write Single Register, 16 Write Multiple Registers, 22 Mask Write, 23 Read/Write Multiple [6, §6.6, §6.12, §6.16–6.17] | rare in measurement (§4) |
| Diagnostics | 08 Diagnostics (sub-functions: 00 loopback, 01 Restart Comm, 11 Bus Message Count, 13 Bus Exception Error Count, ...) | **marked "Serial Line only"** [6, §6.8] |
| File record | 20 Read File Record, 21 Write File Record | legacy/rare on TCP [6, §6.14–6.15] |
| Device identification | 43/0x0E Read Device Identification (MEI type 14; ReadDevId codes 01–04; VendorName/ProductCode/Revision mandatory) | connection-setup/recon material [6, §6.21] |
| Serial-only legacy | 07 Read Exception Status, 11 Get Comm Event Counter, 12 Get Comm Event Log, 17 Report Server ID | all "(Serial Line only)" [6, §6.7–6.13] ⇒ their appearance on TCP is off-spec |
| User-defined | 65–72 and 100–110 | vendor-specific by design [6, §5] |
| Reserved | 9, 10, 13, 14, 41, 42, 90, 91, 125–127; FC8 subcodes 19, 21+ | "shall not be part of this published Specification" [6, Annex A] |

Exception responses: FC+0x80 plus a one-byte exception code — defined in V1.1b3: 01 ILLEGAL
FUNCTION, 02 ILLEGAL DATA ADDRESS, 03 ILLEGAL DATA VALUE, 04 SERVER DEVICE FAILURE, 05 ACKNOWLEDGE,
06 SERVER DEVICE BUSY, 08 MEMORY PARITY ERROR, 0A GATEWAY PATH UNAVAILABLE, 0B GATEWAY TARGET
DEVICE FAILED TO RESPOND (no 07/09) [6, §7]. Normal traffic should have a near-zero exception rate;
bursts of 01/02 are the classic fingerprint of FC/address scanning.

**Seen vs rare in real deployments:** production captures saw essentially only FC 1, 2, 3 (reads)
and 5 (write) [7, §3.1; 2, Table 6]; everything else — writes FC6/16, diagnostics, file record,
device ID — is rare/absent in steady state, which is exactly what makes them high-signal features.

## 4. Measured real Modbus/TCP traffic (core section)

### 4.1 Goldenberg & Wool 2013 (IJCIP) — production campus SCADA *(open copy found)*

Longest published traces from a **production** system: the Modbus/TCP system monitoring the Tel
Aviv University campus power grid (1 HMI + 6 PLCs, 5 active throughout; two capture campaigns five
months apart) [7, §4, §6, Table 2]. **Yes — an open, author-hosted copy of the full published paper
exists** (Wool's old homepage, archived; see Sources [7]); no paywall needed.

| Data set | Duration | Size | Derived aggregate rate |
|---|---|---|---|
| #1 (Jan 2012) | 20 h | 6.3 GB | ≈0.7 Mbit/s |
| #2 (Jun 2012) | 111 h | 35.5 GB | ≈0.7 Mbit/s |

(Rate derived from Table 2 sizes/durations.) Concrete findings:

- **Function codes: only 4 ever seen** — reads FC 1, 2, 3 and write FC 5 [7, §3.1].
- **Traffic is highly periodic per channel**: "the HMI repeatedly polls every PLC at a fixed
  frequency and issues a repeating sequence of commands" [7, §4]. Each HMI–PLC channel is a single
  long-lived TCP connection, restarted immediately on disconnection [7, §3.2, §4].
- **Fixed request sequences (poll tables):** each channel has a small cyclic pattern; learned
  pattern lengths k (symbols = one query or response): PLC#1 16→18 across the 5-month gap, PLC#2
  6→4, PLC#3 6, PLC#4 6, gateway PLC#5 2+2, new PLC#6 6 — i.e., **1–9 query/response pairs per
  cycle** [7, §7.1, Table 3]. The upgrade between captures changed two channels' patterns —
  configuration drift is real and detectable [7, §8].
- **The DFA model learned from ~100 messages** per channel reproduced 111 h of traffic *perfectly*
  for 5 of 7 PLCs — zero unknown messages [7, Abstract, §8].
- Residual anomaly rate is tiny: unknowns ≤0.39 % of packets, 97.7 % of 2-minute time frames
  completely quiet (625/630) [7, §7.3]; the flagged events were technicians troubleshooting the
  HMI and a misconfigured PLC — real operational anomalies, not noise [7, §7.4].
- **Multi-period reality:** beyond the fast poll cycle, the data contained a 24 h pattern (PLC
  counter resets) and a 15 min pattern (averaging a set of control process counters) [7, §8.1] — a slow
  modulation on top of the fast cycle, relevant for simulator design.
- Security-relevant device facts: one tested PLC (Unitronics Vision350) **accepts only a single
  TCP connection on port 502** at a time [7, §3.2].
- Their model symbol = 33 bits: (query/response 1 b, FC 8 b, reference number/start address 16 b,
  bit/word count 8 b) [7, §4.1; 8, §VI] — i.e., the fields that carry the signal are exactly those.

### 4.2 Faisal, Cardenas & Wool 2016 (IEEE CNS) — when periodicity fails *(open copy found)*

Re-analysis of a new one-day trace plus the TAU dataset above (their "dataset #2"): in the one-day
trace, of **94 channels with >1000 packets, only 28 % could be modeled by the DFA learner** (68 %
could not — the authors leave the cause open; cf. the multiplexing caveat [9]); channel data sizes
averaged ~25 MB (max 165 MB) [8, §VI]. FC pair counts in the two example channels: one-day trace
channel 1 — FC3: 1,229,088; FC1: 484,499; FC2: 900 pairs; TAU channel 1 — FC3: 3,392,428 pairs vs
**FC5: 1** [8, Table III]. A simple specification rule ("response later than 5 s") fired 15 times
in the one-day trace's channel 1 [8, §VI]. Takeaway: **reads dominate by ≥3–6 orders of magnitude
over writes, but strict periodicity is per-channel, not universal** — featurize with
per-channel/per-pair baselines rather than assuming one global rhythm.

### 4.3 SCADA World (POMACS 2024) — the protocol-mix context

Six operational power-grid networks (captures 1–111 h): the campus Modbus/TCP network showed FC3
Read Holding Registers 76.9 %, FC1 Read Coils 23.09 %, FC5 <0.001 %; ~50/50 request/response split
(the only classically symmetric poll flow among the studied protocols); Modbus inter-arrival times
1–2 orders of magnitude below other protocols; Modbus/GOOSE/C37.118 formed clear clusters of
identical periods [2, §5.2.2, Table 6, Figs. 7, 10, 17].

### 4.4 Other open measured/testbed sources

- **Lin & Nadjm-Tehrani (CRITIS 2021)** — emulation-vs-real caution quantified (§1); not Modbus but
  the core methodological warning for our simulator [3].
- **Ghosh et al. 2023 (J. Cybersecur. Priv., open access)** — entropy + classifier analysis of
  Modbus/TCP pcaps from Morris & Gao's HVAC lab dataset [10, 11]: univariate/bivariate entropy
  flags DoS (97.87 % classifier accuracy) and partly MITM (82.81 %), but **not reconnaissance** —
  early-stage scanning was invisible to their features [10, Abstract, §VII]. Caution for our
  featurization: recon needs structural features (address/FC coverage, unit-ID sweeps), not just
  volume/timing.
- **Kleinmann & Wool (CRITIS 2015)** — caveat that periodic traffic is "sometimes multiplexed, due
  to asynchronous scheduling" (studied on Siemens S7): single-sequence models then raise false
  alarms; demultiplex into sub-channels first [9, Abstract].

## 5. Typical Modbus communication patterns

| Pattern | Reality | Source |
|---|---|---|
| Master/slave (client/server) semantics | Master (HMI/SCADA) initiates every transaction; slaves only answer — a slave *cannot* spontaneously report (contrast DNP3's unsolicited-reporting mode [4, §Unsolicited]), so values change only when polled | [6, §4.1; 7, §3] |
| Poll tables | Fixed cyclic sequences of (FC, start address, quantity) per slave, repeated indefinitely; 1–9 pairs/cycle measured | [7, §4, Table 3] |
| Poll intervals | Fixed per channel, implementation-specific; production campus traffic ran ≈0.7 Mbit/s aggregate with IATs 1–2 orders below other OT protocols (no absolute ms value published) | [7, Table 2; 2, §5.2.2] |
| Request/response symmetry | ~50/50 by design; each request yields exactly one response (normal or exception) | [2, Fig. 7; 6, §7] |
| Reads dominate, writes rare | FC1/2/3 ≈ 100 % of messages; writes <0.001 %–single digits per millions | [2, Table 6; 7, §3.1; 8, Table III] |
| Concurrency | Recommendation: keep the TCP connection open (don't churn per transaction); open a minimum number of connections; server may close the oldest unused connection when out of connection slots; some PLCs accept only 1 connection at a time | [5, §4.2.1; 7, §3.2] |
| One-transaction-at-a-time | In measured production traffic, strict sequential poll/response on one connection per channel; pipelining is legal but was not the observed norm | [7, §4; 5, §4.2.1 rule 4] |

## 6. Security posture (for realistic attack scenarios)

- **No authentication, no integrity, no encryption**: SCADA components "do not verify the identity
  and permissions of other components ... do not verify message content ... all the data ... is in
  plaintext"; the protocol "does not defend itself in any way against a rogue master" [7, §1, §3.2;
  9, §1]. The application spec defines no security mechanism at all [6].
- First-party mitigation is minimal: the implementation guide's only security feature is an
  **IP-allowlist access-control module** (unknown IPs' connections closed by default in security
  mode) [5, §4.2.3].
- Documented abuse classes (Digital Bond Snort rule taxonomy, as validated by [7, §2]):
  unauthorized Modbus use (writes FC5/6/16 from unexpected masters), protocol errors, and scanning.
  Structural recon surfaces: **unit-ID sweeps** through gateways (any Unit ID 1–247 is routeable
  [5, §4.4.1.3]), FC/address sweeps, and FC43/14 device-identification reads [6, §6.21].
- Availability: single-connection devices can be locked out by occupying port 502 [7, §3.2];
  diagnostics FC8 (Restart Communications, subfunction 01) is serial-only yet restart-capable [6, §6.8].
- Since Modbus has no native auth, extensions have been proposed (e.g., an authentication/
  authorization-enhanced Modbus/TCP variant) — i.e., plain Modbus/TCP remains the deployed
  baseline **(secondary)** [13].

## 7. Featurization mapping for k-means on Modbus/TCP

Primer-style feature groups (counts/rates, ratios, timing) mapped to Modbus fields (§2). Windows of
~10 min; one row per (master, slave) channel where volume allows.

| Feature | Derived from | Primer group | Catches |
|---|---|---|---|
| ADUs/window per channel; total bytes/window | Length field, IP pair | counts/rates | floods, DoS, outage |
| Per-FC counts (esp. FC1/2/3 vs FC5/6/16) | function code | counts/rates | unauthorized writes, off-spec FCs |
| Per-(unit ID, FC) pair counts | Unit ID + FC | counts/rates | unit-ID sweeps, gateway abuse |
| Distinct unit IDs touched/window | Unit ID | counts/rates | unit-ID sweep recon |
| Distinct (FC, start address) pairs; address entropy/coverage | reference number | structure | address scanning (entropy features alone missed recon [10] — use coverage, not just entropy) |
| request:response ratio; exception rate (FC+0x80 share; codes 01/02 vs 04/05/06) | FC byte | ratios | dropped/inserted responses, scanning, failing device |
| TID gap / duplicate / non-monotonic count | transaction ID | structure | replay, injection, hijack (libmodbus counter is deterministic [12]) |
| ADU size mean/max; length-field outliers | Length | counts/rates | oversized craft, new query shapes |
| IAT mean & std per channel | timestamps | timing | machine floods (tiny std), stalls, delay attacks (5 s rule fired in production [8]) |
| Poll-cycle periodicity (autocorrelation / cycle-length fit) | per-channel sequence | timing | out-of-position messages, pattern breaks [7] |
| TCP connection churn (SYNs to :502/window) | TCP header | counts/rates | connection-flooding, single-connection lockout [7, §3.2] |
| Serial-only FCs seen on TCP (07/08/11/12/17) | FC byte | counts/rates | off-spec tooling (spec marks them serial-only [6]) |

Start with ~15 features; every feature must earn its place against the threat model (§6), as in the
primer's rule of thumb.

## 8. Source-quality notes (not verified against primary sources)

- **No absolute poll-interval (ms) number found in open literature** for production Modbus — only
  "fixed frequency" + derived aggregate rates (§4.1, §5). If needed, derive from our own captures.
- **Kleinmann & Wool CRITIS 2015 is S7-based**, not Modbus; cited only for the multiplexing caveat.
- **Morris & Gao's dataset paper is paywalled** [11]; the dataset is characterized here via the
  open-access analysis of it [10] **(secondary)**.
- modbus.org blocks scripted clients (404/403 to curl, fine in browsers); spec content was read
  from Wayback copies of the official PDFs [5, 6] — mirrored in Sources.
- The DNP3 contrast in §5 is from the DNP Users Group primer [4] (dnp.org is the host since
  dnp3.org died); OPC UA / IEC-104 specifics from the predecessor doc were trimmed as not
  Modbus-relevant.

## Sources

1. NIST SP 800-82r3, *Guide to OT Security* (Sep 2023) — §2.3, §2.4, §5.2.3.1/Fig. 16. <https://nvlpubs.nist.gov/nistpubs/specialpublications/NIST.SP.800-82r3.pdf>
2. N. Ortiz, A. A. Cardenas, A. Wool, "SCADA World," *POMACS* 8(1), 2024 (open access; ACM blocks curl — Wayback PDF: <https://web.archive.org/web/2024/https://dl.acm.org/doi/pdf/10.1145/3639036>). <https://doi.org/10.1145/3639036>
3. C.-Y. Lin, S. Nadjm-Tehrani, "A Comparative Analysis of Emulated and Real IEC-104 Spontaneous Traffic...," CRITIS 2021 (open access full text: <https://pmc.ncbi.nlm.nih.gov/articles/PMC7888297/>). <https://doi.org/10.1007/978-3-030-69781-5_14>
4. *DNP3 Primer*, rev. A, DNP Users Group. <https://www.dnp.org/Portals/0/AboutUs/DNP3%20Primer%20Rev%20A.pdf>
5. Modbus Organization, *MODBUS Messaging on TCP/IP Implementation Guide V1.0b* (Oct 2006) — §3.1.2 (ADU 253+7=260 B), §3.1.3 (MBAP fields), §4.2.1 (connection rules), §4.2.3 (access control), §4.4.1.3 (TID pairing; Unit ID 0xFF/0x00). Canonical (bot-blocked): <https://www.modbus.org/docs/Modbus_Messaging_Implementation_Guide_V1_0b.pdf> — Wayback: <https://web.archive.org/web/2023/https://www.modbus.org/docs/Modbus_Messaging_Implementation_Guide_V1_0b.pdf>
6. Modbus Organization, *MODBUS Application Protocol Specification V1.1b3* (Apr 2012) — §4.1 (PDU/FC), §5 (FC categories), §6.1–6.21 (per-FC), §6.8 (FC8 sub-functions), §6.21 (FC43/14), §7 (exception codes), Annex A (reserved). Canonical (bot-blocked): <https://www.modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf> — Wayback: <https://web.archive.org/web/2023/https://www.modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf>
7. N. Goldenberg, A. Wool, "Accurate modeling of Modbus/TCP for intrusion detection in SCADA systems," *IJCIP* 6(2):63–75, 2013. DOI (paywalled): <https://doi.org/10.1016/j.ijcip.2013.05.001> — **open full text (author-hosted, archived):** <https://web.archive.org/web/20170809014546/http://www.eng.tau.ac.il/~yash/1-s2.0-S1874548213000243-main.pdf>
8. M. Faisal, A. A. Cardenas, A. Wool, "Modeling Modbus TCP for Intrusion Detection," IEEE CNS 2016. DOI (paywalled): <https://doi.org/10.1109/CNS.2016.7860524> — **open full text (author-hosted, archived):** <https://web.archive.org/web/20170902050747/http://www.eng.tau.ac.il/~yash/ieeecps2016.pdf>
9. A. Kleinmann, A. Wool, "A Statechart-Based Anomaly Detection Model for Multi-Threaded SCADA Systems," CRITIS 2015, LNCS 9578. DOI (paywalled): <https://doi.org/10.1007/978-3-319-33331-1_11> — **open full text (author-hosted, archived):** <https://web.archive.org/web/20190922015146/https://www.eng.tau.ac.il/~yash/critis2015.pdf>
10. T. Ghosh, S. Bagui, S. Bagui, M. Kadzis, J. Bare, "Anomaly Detection for Modbus over TCP in Control Systems Using Entropy and Classification-Based Analysis," *J. Cybersecur. Priv.* 3(4):896–912, 2023 (open access; MDPI blocks curl — Wayback: <https://web.archive.org/web/2024/https://www.mdpi.com/2624-800X/3/4/41/pdf>). <https://doi.org/10.3390/jcp3040041>
11. T. Morris, W. Gao, "Industrial Control System Traffic Data Sets for Intrusion Detection Research," IFIP ICCIP 2014 (dataset origin; paywalled). <https://doi.org/10.1007/978-3-662-45355-1_5>
12. libmodbus, `src/modbus-tcp.c` (`_modbus_tcp_build_request_basis`: per-connection TID counter, init 0, increment, wrap at 65535→0). <https://github.com/stephane/libmodbus/blob/master/src/modbus-tcp.c>
13. T. Martins, S. V. G. Oliveira, "Enhanced Modbus/TCP Security Protocol: Authentication and Authorization Functions Supported," *Sensors* 22(20):8024, 2022 (open access) **(secondary)**. <https://doi.org/10.3390/s22208024>
14. Eddy (ed.), *RFC 9293: TCP* — §3.8.4 keepalive defaults. <https://www.rfc-editor.org/rfc/rfc9293.html>
