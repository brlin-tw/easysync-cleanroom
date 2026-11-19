# easysync-cleanroom

Specification documents of the Ours Technology Inc.'s EasySync software for their USB host-to-host file transfer products.

<https://gitlab.com/brlin/easysync-cleanroom>  
[![The GitLab CI pipeline status badge of the project's `main` branch](https://gitlab.com/brlin/easysync-cleanroom/badges/main/pipeline.svg?ignore_skipped=true "Click here to check out the comprehensive status of the GitLab CI pipelines")](https://gitlab.com/brlin/easysync-cleanroom/-/pipelines) [![GitHub Actions workflow status badge](https://github.com/brlin-tw/easysync-cleanroom/actions/workflows/check-potential-problems.yml/badge.svg "GitHub Actions workflow status")](https://github.com/brlin-tw/easysync-cleanroom/actions/workflows/check-potential-problems.yml) [![pre-commit enabled badge](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white "This project uses pre-commit to check potential problems")](https://pre-commit.com/) [![REUSE Specification compliance badge](https://api.reuse.software/badge/gitlab.com/brlin/easysync-cleanroom "This project complies to the REUSE specification to decrease software licensing costs")](https://api.reuse.software/info/gitlab.com/brlin/easysync-cleanroom)

This project specifically targets Oti-2108 USB host-to-host file transfer cables, other models may require different handling or may not be supported at all.

## Clean-Room Specification Suite (`specs/`)

These five specification documents contain all byte-level framing, state machines, command opcodes, XML schemas, and UI functional requirements needed to build a clean-room, cross-platform implementation without accessing any proprietary binaries:

* [Overview & System Architecture](specs/00-overview-and-architecture.md)  
  Hardware identity (`0ea0:2108`), self-extracting container inventory, dual-mode architecture (**Transfer Line Mode `0x007F`** vs **Virtual NIC Mode `0x0044`**), cross-platform architecture blueprint, and clean-room firewall rules.
* [USB & SCSI Pass-Through Transport](specs/01-usb-and-scsi-transport-spec.md)  
  Dual-backend transport abstraction (direct `libusb` Bulk-Only Transport `USBC`/`USBS` with tag `0x08210827` and OS SCSI Pass-Through `SG_IO`/`SPTI`), all 10 SCSI/vendor CDBs (`0x00`, `0x28`, `0xF0`, `0xD8 0x00`, `0xD8 0x01`, `0xD9 0x28`, `0xD9 0x2A`, `0xDA 0x28`, `0xDA 0x2A`, `0xDB 0x28`), 24-bit sense code mapping, 8-state poll loop (`action_code 0..7`), and verified physical hardware captures.
* [Host-to-Host Wire Protocol](specs/02-host-to-host-wire-protocol-spec.md)  
  64 KB (`0x10000`) bulk block framing (20-byte little-endian header + `0xFFEC` payload), multi-block packet splitter/reassembler, all 25 Client Command opcodes (`0x01..0x17`, `0x32..0x39`), all 12 Server Reply opcodes (`0xA1..0xAC`), 552-byte (`0x228`) directory entry layout, and `<OTIMSG>` / `<ExtraXmlCommand>` XML schemas.
* [Application Functional & UI Specification](specs/03-application-functional-and-ui-spec.md)  
  Dual-pane Local/Remote file manager UI, 8 menus, 7 dialogs, 11 language locales, folder synchronization engine (`My Briefcase`), conflict resolution workflows, and legacy plugin boundaries (`ECN001.DLL` Outlook sync and `ProdLic.dll` flash license check).
* [Virtual NIC & Mode-Switch Protocol](specs/04-virtual-nic-and-mode-switch-spec.md)  
  Mode switching (`0x007F` $\leftrightarrow$ `0x0044`), `35,328`-byte (`0x8A00`) Ethernet batch buffer framing (21-byte header + `[frame_len_u16_le][eth_frame]` stream + `0xFFFF` terminator), synthetic MAC assignment (`00:10:60:21:08:01`), and cross-platform TAP/TUN daemon design.

## Non-Destructive Hardware Probe (`tools/probe_oti2108_hw.py`)

Queries an attached OTi-2108 (`0ea0:2108`) cable in strictly read-only mode (`0xF0` Identify, `0xD8 0x00` status registers `op_type=1, 2, 3, 5`, and `0xDB 0x28` flash config) via either direct `libusb` Bulk-Only Transport or Linux `SG_IO`:

```bash
# Offline self-test against synthetic & hardware vectors
python3 tools/probe_oti2108_hw.py --dry-run

# Live hardware probe via direct libusb Bulk-Only Transport
sudo python3 tools/probe_oti2108_hw.py --usb

# Live hardware probe via Linux SG_IO pass-through
sudo python3 tools/probe_oti2108_hw.py --sg-dev /dev/sgX
```

Archived hardware probe outputs from a physical OTi-2108 unit (`chip_bcd = 2108`, `firmware_rev = 3`, build date `"030729"`, 512 KB buffer):
- [`dumps/probe_oti2108_hw.libusb.out.txt`](dumps/probe_oti2108_hw.libusb.out.txt)
- [`dumps/probe_oti2108_hw.sg.out.txt`](dumps/probe_oti2108_hw.sg.out.txt)

## Licensing

Unless otherwise noted([comment headers](https://reuse.software/spec-3.3/#comment-headers)/[REUSE.toml](https://reuse.software/spec-3.3/#reusetoml)), this product is licensed under [the 4.0 International version of the Creative Commons Attributions-SharedAlike license](https://creativecommons.org/licenses/by-sa/4.0/), or any of its more recent versions of your preference.

This work complies to [the REUSE Specification](https://reuse.software/spec/), refer to the [REUSE - Make licensing easy for everyone](https://reuse.software/) website for info regarding the licensing of this product.
