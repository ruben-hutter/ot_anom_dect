# Research: What real OT traffic looks like (quantitative profiles)

*Purpose: answer "what packets/messages/signals does an OT network actually carry, and in what
quantities?" so the lab can generate traffic that a k-means baseline can honestly learn from.
Complements [primer-dbus-kmeans.md](primer-dbus-kmeans.md), which covers D-Bus message anatomy,
featurization (§3) and k-means (§4) — this doc stays on the OT side and ends with a mapping
(§5) back to that featurization. Method: primary sources only (specs, standards bodies, NIST,
first-party vendor docs, peer-reviewed measurement studies); every number carries a citation
`[n]` into [Sources](#sources). Secondary sources are marked **(secondary)**.*

---

## 1. Reference architecture: where traffic comes from

The standard segmentation model is the **Purdue model** (levels 0–4/5), which NIST SP 800-82r3
adopts alongside ISA-95/IEC 62264 levels and recommends (with a DMZ as enforcement boundary)
for OT segmentation [1, §5.2.3.1, Fig. 16]. NIST's concrete rule of thumb: firewall policies
should prevent Level 4 devices from directly communicating with Level 2, 1, or 0 — traffic
crosses levels only through adjacent hops [1, §5.2.3.1].

| Purdue level | What lives there | Traffic it generates | Direction |
|---|---|---|---|
| L0 | sensors, actuators, drives | analog/digital I/O (often fieldbus, not IP) | E-W (device-level) |
| L1 | PLCs, RTUs, protective relays | cyclic I/O scans, event/RBE reports to L2 | N-S (up), E-W (peer) |
| L2 | HMIs, SCADA servers/control room | polling + setpoint writes (down), alarms (up) | N-S |
| L3 | site ops: history, engineering, MES | config/program downloads, historian writes, diagnostics | N-S + E-W |
| L3.5 | OT DMZ | replicated data, patches, updates | controlled N-S |
| L4/5 | business (ERP, office) | rare, mediated via DMZ | N-S |

Measured protocol placement in a real power grid (one of the few multi-network studies):
IEC 104 connects control rooms to generation plants and distribution substations; ICCP
synchronizes control centers with each other; GOOSE runs IED-to-IED inside substations;
IEEE C37.118 carries PMU data; Modbus/TCP monitored a consumer (campus) site [2, Table 1–2,
§3.4]. Timing context: supervisory and lower-level control loops "operate continuously ...
with cycle times ranging in the order of milliseconds to minutes" [1, §2.3], and many OT
processes run continuously — outages must be scheduled days or weeks ahead [1, §2.4].

## 2. Canonical traffic categories, with numbers

| Category | Typical interval / rate | Typical size | Direction | Evidence |
|---|---|---|---|---|
| Cyclic polling (master ↔ RTU/PLC) | per-point/scan: ms–minutes [1, §2.3]; measured campus Modbus: highest packet rate of all studied networks (IAT 1–2 orders below other protocols) [2, §5.2.2] | request/response pairs; Modbus ADU ≤ 260 B [6, §3.1.2] | ~50 % each way (request/response doubles volume) [2, Fig. 7] | [1, 2, 6] |
| Event / report-by-exception | real RTU: 13 981 and 401 spontaneous IEC-104 events per 30 days (~19/h and ~0.6/h) [3, Table 1]; deadband- or change-triggered [3, §Data] | small: IEC-104 typical payload 36 B / 2 data points [2, Fig. 10] | field → master (agent-to-controller) [2] | [2, 3, 4] |
| Alarms & sequence-of-events | rare; time-tagged binary events 4.4 % of ASDUs in a distribution network (M_SP_TB_1), <0.001 % in a bulk network [2, Tables 4–5] | small | field → master | [2, 4] |
| Time sync | NTP: client poll 64–1024 s by default [10, §7.2; 12]; protocol-level: IEC-104 C_CS_NA_1 0.0015 %–0.25 % of ASDUs [2, Tables 4–5]; PTP: 1 sync/s default, 8/s in gPTP [13] | tiny (≤ ~50 B payloads) | master → field (or NTP client ↔ server) | [2, 10, 12, 13] |
| Heartbeats / keepalives | IEC-104 TESTFR when idle ≥ t3 = 20 s [9]; OPC UA keep-alive = maxKeepAliveCount × publishingInterval [8, §5.14.1]; TCP keepalive default ≥ 2 h, off by default [11, §3.8.4]; GOOSE: only regular heartbeats in 14 h of quiet capture [2, §5.2.2] | tiny | mostly field → master (status) | [2, 8, 9, 11] |
| Configuration / program / firmware | rare, bursty, operator-initiated (engineering stations at L3) [1, §2.3]; no public measured rate found (see §6) | large (whole files) — the measured outlier class is ICCP control-center sync: 443 B payload / 11 data points vs 36 B typical [2, Fig. 10] | L3 → field (down) | [1, 2] |
| Operator / HMI reads & writes | steady-state writes are rare: setpoint commands C_SE_NC_1 = 0.845 % of bulk-network ASDUs [2, Table 4]; Modbus write (FC5) < 0.001 % of messages [2, Table 6] | small | master → field | [1, §2.3; 2] |
| Diagnostics / metadata | DNP3 class 0 "integrity poll" only occasionally [4]; measured: passive substations interrogated every 2 min (~120 pkt/h each) [2, Table 7]; OPC UA Browse (§5.9) at session start [8] | bursts of small reads | master → field | [2, 4, 8] |

Category notes worth keeping:

- **Polling is not the whole story.** The assumption "SCADA traffic is periodic because of
  polling" is a documented misconception: in the measured networks, request/response was the
  *least*-used reporting mechanism; spontaneous and periodic (unsolicited) reporting dominated,
  and only Modbus produced a classic symmetric poll flow [2, §6]. DNP3 supports both modes:
  masters poll for events, or outstations report unsolicited responses as soon as a change
  occurs [4, §Polling, §Unsolicited Responses].
- **Event classes:** DNP3 classifies events into classes 1 > 2 > 3, with class 0 being the
  static dataset fetched occasionally as an integrity measure [4, §Classes]. IEC-104
  spontaneous traffic is deadband-driven: analog points report on threshold crossing, binary
  points on change [3, §Data].
- **Master/field flow asymmetry varies by protocol role:** Modbus ~50/50, C37.118 ~100 %
  field→master, ICCP control-center sync is majority master→agent [2, Fig. 7].

## 3. Protocol message profiles

### 3.1 Modbus/TCP

- Request/response only — an agent *cannot* spontaneously report; the controller must keep
  asking, which doubles traffic vs reporting protocols [2, §5.2.2].
- ADU = PDU + MBAP header; max PDU 253 B ⇒ max TCP ADU = 253 + 7 = 260 B [6, §3.1.2];
  MBAP header is 7 bytes and all MODBUS/TCP ADUs go to registered port 502 [6, §3.1.3].
- Function-code mix measured over 111 h on a campus network: FC3 Read Holding Register 76.9 %,
  FC1 Read Coils 23.09 %, FC5 Write Single Coil < 0.001 % [2, Table 6].

### 3.2 DNP3 (IEEE 1815 [5])

- Link-layer frame ≤ 292 octets (incl. header/CRC); a transport frame carries ≤ 249 application
  octets; application fragments 2048–4096 B (a 2048 B fragment = 9 link frames) [4, §Frames].
- Event classes 1/2/3 (priority order), class 0 static/integrity poll, and an unsolicited
  mode where outstations report changes immediately [4, §Classes, §Unsolicited].
- Addresses are 2-byte static device addresses; confirms are optional per-layer [4, §Frames].

### 3.3 IEC 60870-5-104

- TCP port 2404. Frame types: I-format (data, sequence numbered), S-format (ack), U-format
  (control: STARTDT/STOPDT/TESTFR) [9, §Protocol overview].
- Standard parameters: t0 = 30 s (connect), t1 = 15 s (ack/STARTDT/TESTFR timeout), t2 = 10 s
  (S-frame sent at latest), t3 = 20 s (TESTFR when idle), k = 12, w = 8; max APDU = 253 B
  (max ASDU 249 B) [9; standard record: 18]. Corroborated by the lib60870 implementation defaults
  (t1 = 15, t2 = 10, t3 = 20) [16].
- Measured in a distribution network: > 50 % of substations "passive" (U-format heartbeats
  only); ack cadence follows w = 8 for active agents; passive agents get a full interrogation
  every 2 min (~120 pkt/h); active agents emit 5.4–7.3k I-format packets/h [2, §6, Table 7].
- ASDU shares — bulk network: 72.4 % M_ME_TF_1 (float w/ time tag), 26.5 % M_ME_NC_1, 0.845 %
  C_SE_NC_1 (setpoint), 0.0035 % C_IC_NA_1 (interrogation), 0.0015 % C_CS_NA_1 (clock sync);
  distribution network: 50.8 % M_ME_NA_1, 42.3 % M_ME_TF_1, 4.39 % M_SP_TB_1 (time-tagged
  single point), 2.19 % C_IC_NA_1, 0.246 % C_CS_NA_1 [2, Tables 4–5].

### 3.4 OPC UA

- Clients subscribe: the server checks a subscription every **publishingInterval** (client-
  requested, server may revise; it also defaults the sampling interval of the subscription's
  monitored items) [8, §5.14.1, §5.14.2.2].
- A **keep-alive counter** counts consecutive publishing cycles without notifications; at
  maxKeepAliveCount the server answers a queued Publish request with a keep-alive message so
  the client knows the subscription lives [8, §5.14.1].
- The subscription's **lifetime counter** (no Publish requests available) must be ≥ 3 × the
  keep-alive count, after which the server deletes the subscription and sends a
  StatusChangeNotification [8, §5.14.1, §5.14.2.2].
- Diagnostics/metadata = View service set (Browse/BrowseNext/TranslateBrowsePaths) [8, §5.9].

### 3.5 Short notes: PROFINET and EtherNet/IP

- **PROFINET RT**: cyclic UDP telegrams between controller and devices; the update time of the
  cyclic process-data exchange is the central configuration parameter, computed from controller
  and device cycle times [15, §Timing] **(secondary)**. PI documents cycle times down to
  31.25 µs with its newer performance upgrades (fragmentation, Dynamic Frame Packing) [14].
  Telegrams are small and strictly periodic; alarms use a separate acyclic channel.
- **EtherNet/IP**: cyclic data moves over CIP produced/consumed connections at a per-connection
  configured RPI (requested packet interval); Rockwell's network-configuration manual documents
  RPI-based I/O connections [17]. No primary quantitative RPI distribution found (see §6).

## 4. Measured characteristics from real deployments

Main source: Ortiz, Cardenas & Wool 2024 — six operational power-grid networks (generation,
transmission, distribution, consumer; 6–36 hosts; captures 1–111 h; 42.7 MB–35.5 GB) [2,
Table 2]. Secondary: Lin & Nadjm-Tehrani 2021, two real utility RTUs over 30 days [3].

- **Packet sizes are small and concentrated**: ~75 % of packets in *all* studied networks are
  < 100 bytes; typical IEC-104 data packet payload 36 B (2 data points, one substation) [2, §5.2.2].
- **Outliers are the metadata-heavy flows**: ICCP (control-center state sync) had the largest
  and most variable packets (443 B payload, 11 data points, multiple substations per packet) [2, §5.2.2].
- **Regularity splits by mechanism**: GOOSE, Modbus and C37.118 formed clear clusters of
  identical periods ("periodic"); IEC-104 spontaneous traffic is aperiodic and varies with the
  state of the grid; GOOSE/ICCP/IEC-104 inter-arrival CDFs show a transition near the 1 s mark [2, §5.2.2, §6].
- **Volume hierarchy**: bulk-grid WANs carry more per-connection data than distribution;
  a distribution network's passive substations emit only heartbeats (~120 pkt/h under 2-min
  interrogation) while active ones emit thousands of packets/h [2, §5.2.2, Table 7].
- **Real event rates are low**: 0.6–19 spontaneous IEC-104 events/h per real RTU (30-day
  captures), so sparse that the authors excluded flows under 0.3 events/h from analysis [3, Table 1].
- **Emulation caution**: of three traffic characteristics observed in emulated/testbed traffic,
  two appeared in real traffic "in a less regular way" and one did not appear at all — emulated
  setups (16.9–67 events/h) over-generate and over-regularize relative to real RTUs [3, §Abstract/Conclusions, Table 1].
- **Writes/commands are a rounding error** in steady state (≤ ~0.85 % of ASDUs; Modbus writes
  < 0.001 %) [2, Tables 4–6]; the only studied network sending grid-changing control commands
  was the bulk IEC-104 one [2, Fig. 17].
- **Diurnal patterns**: public quantitative evidence is thin (the above captures are 1–111 h).
  NIST: many OT processes are continuous with outage windows scheduled days/weeks ahead [1,
  §2.4]; SCADA World documents daily topology/traffic change from energy dispatch (generation
  nodes added/removed with demand) [2, §6]. Practically: expect a 24/7 baseline with slow daily
  modulation, plus shift-driven HMI bursts in manufacturing-style sites (qualitative).

## 5. Mapping to the lab: emulating OT traffic with D-Bus

Each category → D-Bus message shape, rate guidance, and which primer §3 feature group it
exercises. Simulated clock (`data/clock.json`) drives hour-of-day; all intervals should carry
jitter (see §4 emulation caution).

| OT category | D-Bus emulation | Rate / size | Primer §3 features exercised |
|---|---|---|---|
| Cyclic polling | `METHOD_CALL` (`sensors.ReadValue`) + `METHOD_RETURN` pairs | 1–5 s per client, jittered; bodies < 100 B | counts/rates, call:return ratio, IAT mean + *low* std |
| Report-by-exception | `SIGNAL` (`sensors.ThresholdCrossed` / PropertiesChanged-style) on deadband | sparse: ~0.6–20 events/h/source; cluster bursts | signal share, IAT std (high), per-interface counts |
| Alarms & SOE | `SIGNAL` (`alarms.Alarm(u time, y code)`) | rare, bursty; time-tagged body | per-member counts, burst detection |
| Time sync | tiny `METHOD_CALL` (`time.Sync`), or periodic signal | every 64–1024 s (NTP-like) [10, 12]; tiny body | timing features, body-length floor |
| Heartbeats | empty-body `SIGNAL` (`health.Heartbeat`); plus `METHOD_CALL` TESTFR-like ping every ~20 s idle | 1–10 s per client | messages/window floor, unique senders, IAT |
| Config / firmware | `METHOD_CALL` (`admin.UploadFirmware`) with KB-sized array body, rare | 1×/sim-day-ish, bursty | avg/max body length, bytes/window spike |
| HMI reads/writes | bursts of `Get`/`Set` calls during sim-hours 8–17; writes < 1 % of messages | bursty, diurnal | hour sin/cos, per-interface counts, `NO_REPLY_EXPECTED` share |
| Diagnostics / metadata | `Introspect`/`GetManagedObjects` call bursts; `Hello`/`AddMatch`/`RequestName` on "RTU join" | on connect + occasional | bus-meta counts, new-sender, distinct (interface, member) pairs |

Correspondence with real-protocol mechanics: `AddMatch` ≈ OPC UA subscription creation (the
client opts into notifications) [8, §5.14.1]; `RequestName` ≈ a DNP3 outstation/RTU registering
its identity [4]; a passive agent = a D-Bus client that only connects, heartbeats, and answers
2-min full-snapshot polls [2, Table 7].

**Sanity numbers for the simulator** (5 legitimate clients): heartbeat floor ≈ 1–5 msg/s total,
polling pairs ≈ 1–3 msg/s ⇒ ~200–700k msgs/sim-day dominated by tiny messages; events sparse
(≈ 0–3 per 10-min window per client); bodies < 100 B except rare firmware bursts — consistent
with the measured size distribution [2]. Keep > 50 % of clients "passive" (heartbeat-only) to
match the distribution-network measurement [2].

**Make it messy (from §4):** jitter every interval; use deadband semantics so sensor signals
fire only on change; include near-silent flows (< 0.3 events/h); keep writes rare; put HMI
bursts in day shift hours and leave nights to heartbeats; let error replies stay exceptional.

---

## 6. Source-quality notes (things I could not verify against a primary source)

- **dnp3.org is dead** (expired TLS cert since 2022, parked); the DNP3 Users Group site
  (dnp.org) hosts the protocol primer and is used here instead. IEEE 1815 = DNP3 [5].
- **modbus.org blocks scripted clients** (403/404 to curl, fine in browsers); the cited spec
  numbers were read from Wayback copies of the official PDFs [6, 7].
- **Goldenberg & Wool 2013 / Kleinmann & Wool** (real water-utility Modbus traffic, IJCIP)
  are paywalled with no open copy found, so **no numbers from them are quoted** here; the
  measured-data role is filled by the open-access [2] and [3].
- **No public primary source found for**: typical OPC UA publishing-interval values in real
  deployments (spec mechanism cited instead [8]), firmware/config transfer rates and sizes
  (qualitative only [1]), alarm-rate guidelines (ISA-18.2/EEMUA 191 are paywalled),
  EtherNet/IP RPI distributions [17], and quantitative diurnal/shift traffic patterns.

---

## Sources

1. NIST SP 800-82r3, *Guide to Operational Technology (OT) Security* (Sep 2023) — §2.3 (control loops, ms–min), §2.4 (continuous operation), §5.2.3.1/Fig. 16 (Purdue + DMZ), §5.3.6 (Level 0). <https://nvlpubs.nist.gov/nistpubs/specialpublications/NIST.SP.800-82r3.pdf>
2. N. Ortiz, A. A. Cardenas, A. Wool, "SCADA World: An Exploration of the Diversity in Power Grid Networks," *Proc. ACM Meas. Anal. Comput. Syst.* 8(1), Art. 10, 2024 (open access; ACM blocks scripted clients — archived PDF: <https://web.archive.org/web/2024/https://dl.acm.org/doi/pdf/10.1145/3639036>). <https://doi.org/10.1145/3639036>
3. C.-Y. Lin, S. Nadjm-Tehrani, "A Comparative Analysis of Emulated and Real IEC-104 Spontaneous Traffic in Power System Networks," CRITIS 2021, LNCS 12618, pp. 207–223 (open access full text: <https://pmc.ncbi.nlm.nih.gov/articles/PMC7888297/>). <https://doi.org/10.1007/978-3-030-69781-5_14>
4. *DNP3 Primer*, rev. A, DNP Users Group (first-party protocol primer). <https://www.dnp.org/Portals/0/AboutUs/DNP3%20Primer%20Rev%20A.pdf>
5. IEEE Std 1815 (DNP3) standards page. <https://standards.ieee.org/ieee/1815/5841/>
6. Modbus Organization, *MODBUS Messaging on TCP/IP Implementation Guide V1.0b* — §3.1.2 (ADU = 253 + 7 = 260 B), §3.1.3 (7-byte MBAP), port 502. Canonical: <https://www.modbus.org/docs/Modbus_Messaging_Implementation_Guide_V1_0b.pdf> (bot-blocked to curl; verified via Wayback: <https://web.archive.org/web/2023/https://www.modbus.org/docs/Modbus_Messaging_Implementation_Guide_V1_0b.pdf>)
7. Modbus Organization, *MODBUS Application Protocol Specification V1.1b3* — PDU ≤ 253 B. Canonical: <https://www.modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf> (verified via Wayback: <https://web.archive.org/web/2023/https://www.modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf>)
8. OPC Foundation, *OPC Unified Architecture Part 4, v105* — §5.14.1 (Subscription model: publishingInterval, keep-alive counter, lifetime ≥ 3× keep-alive), §5.14.2.2 (CreateSubscription parameters), §5.9 (View/Browse). <https://reference.opcfoundation.org/Core/Part4/v105/docs/5.14.1> (§5.14.2.2, §5.9: same docs tree)
9. Beckhoff Information System, *TF6500 TC3 IEC 60870-5-10x* (first-party implementation doc) — t0/t1/t2/t3/k/w defaults and meanings, port 2404, APDU 253 / ASDU 249 derivation. <https://infosys.beckhoff.com/content/1033/tf6500_tc3_iec60870_5_10x/984053899.html> and <https://infosys.beckhoff.com/content/1033/tf6500_tc3_iec60870_5_10x/983963275.html>
10. Mills et al., *RFC 5905: Network Time Protocol Version 4* — §7.2 global parameters (MINPOLL 4 = 16 s, MAXPOLL 17 = 36 h; reference implementation MINPOLL 6 = 64 s). <https://www.rfc-editor.org/rfc/rfc5905.html>
11. Eddy (ed.), *RFC 9293: Transmission Control Protocol (TCP)* — §3.8.4 keep-alive: configurable, MUST default ≥ 2 h, MUST default off. <https://www.rfc-editor.org/rfc/rfc9293.html>
12. chrony documentation, *chrony.conf(5)* — `minpoll` default 6 (64 s), `maxpoll` default 10 (1024 s). <https://chrony-project.org/doc/latest/chrony.conf.html>
13. linuxptp (reference open-source PTP implementation, maintainer M. Lichvar) — default `logSyncInterval 0` (1 sync/s), `logAnnounceInterval 1` (2 s) in `config.c`; gPTP profile `configs/gPTP.cfg` uses `logSyncInterval -3` (125 ms). <https://github.com/mlichvar/linuxptp>
14. PROFIBUS & PROFINET International (PI), PROFINET technology pages — cycle times down to 31.25 µs with fragmentation/Dynamic Frame Packing. <https://www.profibus.com/profinet-technology>
15. M. Felser, *The PROFINET Manual*, "Planning of an IO-System: Define Timing" (update time = f(controller/device cycle times)) **(secondary, practitioner)**. <https://felser.ch/profinet-manual/t_-_timing.html>
16. MZ Automation, lib60870 (open-source IEC 101/104 implementation) — CS104 connection defaults t1 = 15, t2 = 10, t3 = 20 s (`cs104_connection.c`). <https://github.com/mz-automation/lib60870>
17. Rockwell Automation, *EtherNet/IP Network Configuration User Manual* (ENET-UM001, produced/consumed connections & RPI). <https://literature.rockwellautomation.com/idc/groups/literature/documents/um/enet-um001_-en-p.pdf>
18. IEC, *IEC 60870-5-104:2006* standard record (values cited per [9]). <https://webstore.iec.ch/en/publication/3746>
