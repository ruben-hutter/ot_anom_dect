# Official Modbus specifications (ground truth)

Vendored copies of the official Modbus Organization specification PDFs. These
are the authoritative reference for all protocol work in this repo: the
hello-world lab traffic, the protocol primer, and later featurization.

- Source page: <https://www.modbus.org/modbus-specifications> (official spec list)
- Original file URLs: `https://www.modbus.org/file/secure/<name>.pdf` with

  | `<name>` | local file |
  |---|---|
  | `modbusprotocolspecification` | [modbus-application-protocol-v1.1b3.pdf](modbus-application-protocol-v1.1b3.pdf) |
  | `messagingimplementationguide` | [modbus-tcp-messaging-implementation-guide-v1.0b.pdf](modbus-tcp-messaging-implementation-guide-v1.0b.pdf) |
  | `modbussecurityprotocol` | [modbus-tcp-security-protocol-2021.pdf](modbus-tcp-security-protocol-2021.pdf) |
  | `modbusoverserial` | [modbus-over-serial-line-v1.02.pdf](modbus-over-serial-line-v1.02.pdf) |

## Provenance

modbus.org sits behind an Azure WAF that blocks scripted access, so the PDFs
were retrieved from the Internet Archive as **original-byte `id_` snapshots**
(all retrieved **2026-10-29**). Exact snapshot URLs:

- Application protocol:
  <https://web.archive.org/web/20261010081829id_/https://www.modbus.org/file/secure/modbusprotocolspecification.pdf>
- TCP messaging guide:
  <https://web.archive.org/web/20261023094718id_/https://www.modbus.org/file/secure/messagingimplementationguide.pdf>
- Security protocol:
  <https://web.archive.org/web/20260716211853id_/https://www.modbus.org/file/secure/modbussecurityprotocol.pdf>
- Serial line:
  <https://web.archive.org/web/20261018093547id_/https://www.modbus.org/file/secure/modbusoverserial.pdf>

## The documents

| File | Official title / version | Covers | Grounds |
|---|---|---|---|
| `modbus-application-protocol-v1.1b3.pdf` | MODBUS Application Protocol Specification V1.1b3 (50 pp) | The protocol proper: function codes, PDU/ADU layout, exception responses, data encodings | Lab traffic, primer, featurization |
| `modbus-tcp-messaging-implementation-guide-v1.0b.pdf` | MODBUS Messaging on TCP/IP Implementation Guide V1.0b (54 pp) | MBAP header, TCP port 502, transaction/state machines, framing on TCP | Lab traffic, primer, featurization |
| `modbus-over-serial-line-v1.02.pdf` | MODBUS over Serial Line Specification and Implementation Guide V1.02 | RTU/ASCII framing, addressing, timing on RS-485/232 serial lines | Primer background, featurization of serial captures |
| `modbus-tcp-security-protocol-2021.pdf` | MODBUS/TCP Security Protocol Specification, MB-TCP-Security-v36, 2021-07-30 | TLS-based security (MBAP security extensions) for MODBUS/TCP | Threat-model reference only — **not** used by the hello-world lab |
