#!/usr/bin/env python3
"""
Non-destructive, read-only hardware probe for the OTi-2108 USB Host-to-Host Data Bridge
(USB VID:PID 0ea0:2108, Ours Technology Inc. / UR Technology Inc.).

Supports:

1. Linux SG_IO SCSI pass-through (--sg-dev /dev/sgX)
2. Direct USB Bulk-Only Transport (BOT) via PyUSB/libusb (--usb)
3. Offline self-test / dry-run mode (--dry-run) validating all CDBs, CBWs, CSWs,
    sense code decoders, and response parsers against EasySuite.exe / usbnic.sys rules.

Strictly read-only: sends ONLY 0xF0 (Identify), 0xD8 0x00 (Read Status op_type=1,2,3,5),
and optionally 0xDB 0x28 (Read Flash Config at 0x0022). Never writes to registers or pipes.

Copyright 2026 林博仁(Buo-ren Lin) <buo.ren.lin@gmail.com>
SPDX-License-Identifier: CC-BY-SA-4.0+
"""

import argparse
import ctypes
import fcntl
import json
import os
import struct
import sys
from typing import Callable


USB_VID_OTI = 0x0EA0
USB_PID_2108 = 0x2108

# USB Mass Storage Bulk-Only Transport (BOT) constants from usbnic.sys (0x0001199C..0x00011C60)
USBC_SIGNATURE = 0x43425355  # "USBC" little-endian
USBS_SIGNATURE = 0x53425355  # "USBS" little-endian
OTI_CBW_TAG = 0x08210827     # Constant dCBWTag used by usbnic.sys

# Linux SG_IO ioctl constants
SG_IO = 0x2285
SG_DXFER_NONE = -1
SG_DXFER_FROM_DEV = -3


class SgIoHdr(ctypes.Structure):
    """Linux sg_io_hdr_t structure (64-bit / 32-bit native layout)."""
    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


def build_cdb_identify() -> bytes:
    """Build 16-byte OTi-2108 Identify CDB (0xF0) from EasySuite.exe 0x00431EDF / usbnic.sys 0x0001199C."""
    cdb = bytearray(16)
    cdb[0] = 0xF0
    cdb[14] = 0x4F  # 'O'
    cdb[15] = 0x54  # 'T'
    return bytes(cdb)


def build_cdb_read_status(op_type: int, param_u16: int = 0) -> bytes:
    """Build 16-byte OTi-2108 Read Bridge Status CDB (0xD8 0x00) from EasySuite.exe 0x00432007."""
    cdb = bytearray(16)
    cdb[0] = 0xD8
    cdb[1] = 0x00
    cdb[2] = op_type & 0xFF
    cdb[3] = (param_u16 >> 8) & 0xFF
    cdb[4] = param_u16 & 0xFF
    cdb[14] = 0x4F  # 'O'
    cdb[15] = 0x54  # 'T'
    return bytes(cdb)


def build_cdb_read_flash_config(addr_u16: int = 0x0022, blocks: int = 1) -> bytes:
    """Build 16-byte OTi-2108 Read Flash Config CDB (0xDB 0x28) from EasySuite.exe 0x004328FE."""
    cdb = bytearray(16)
    cdb[0] = 0xDB
    cdb[1] = 0x28
    cdb[4] = (addr_u16 >> 8) & 0xFF
    cdb[5] = addr_u16 & 0xFF
    cdb[7] = (blocks >> 8) & 0xFF
    cdb[8] = blocks & 0xFF
    cdb[14] = 0x4F  # 'O'
    cdb[15] = 0x54  # 'T'
    return bytes(cdb)


def wrap_bot_cbw(cdb: bytes, transfer_length: int, direction_in: bool = True, tag: int = OTI_CBW_TAG) -> bytes:
    """Wrap a 16-byte OTi-2108 CDB in a 31-byte USB Mass Storage BOT Command Block Wrapper (CBW)."""
    assert len(cdb) <= 16
    cdb_padded = cdb.ljust(16, b"\x00")
    flags = 0x80 if direction_in and transfer_length > 0 else 0x00
    header = struct.pack("<IIIBBB", USBC_SIGNATURE, tag, transfer_length, flags, 0x00, 16)
    return header + cdb_padded


def parse_bot_csw(csw: bytes, expected_tag: int | None = OTI_CBW_TAG) -> dict:
    """Parse a 13-byte USB Mass Storage BOT Command Status Wrapper (CSW)."""
    if len(csw) != 13:
        raise ValueError(f"Expected 13-byte CSW, got {len(csw)} bytes")
    sig, tag, residue, status = struct.unpack("<IIIB", csw)
    if sig != USBS_SIGNATURE:
        raise ValueError(f"Invalid CSW signature: 0x{sig:08x} (expected 0x{USBS_SIGNATURE:08x})")
    if expected_tag is not None and tag != expected_tag:
        raise ValueError(f"CSW tag mismatch: 0x{tag:08x} != 0x{expected_tag:08x}")
    return {
        "signature": "USBS",
        "tag": f"0x{tag:08x}",
        "data_residue": residue,
        "status": status,
        "status_ok": status == 0,
    }


def decode_oti_sense(sense_key: int, asc: int, ascq: int) -> dict:
    """
    Decode SCSI Sense Key / ASC / ASCQ using EasySuite.exe's exact lookup table at 0x00431ACF.
    Combines (sense_key & 0x0F) << 16 | (asc << 8) | ascq into a 24-bit code.
    """
    packed = ((sense_key & 0x0F) << 16) | ((asc & 0xFF) << 8) | (ascq & 0xFF)
    mapping = {
        0x000000: (0x04, "No Sense / OK"),
        0x023A00: (0x05, "Medium Not Present"),
        0x052000: (0x06, "Invalid Command Operation Code"),
        0x052400: (0x07, "Invalid Field in CDB"),
        0x062800: (0x08, "Not Ready to Ready Transition (Medium Changed)"),
        0x063F0E: (0x09, "Reported LUNs Data Has Changed"),
        0x072700: (0x10, "Write Protected"),
        0x098000: (0x0A, "OTi Vendor Error 0x80 (Pipe/Transfer Fault)"),
        0x098100: (0x0B, "OTi Vendor Error 0x81 (Peer Disconnected / Timeout)"),
        0x098200: (0x0F, "OTi Vendor Error 0x82 (Buffer Overflow / Busy)"),
        0x098300: (0x12, "OTi Vendor Error 0x83 (Sequence / State Mismatch)"),
        0x098400: (0x0D, "OTi Vendor Error 0x84 (Peer Reset / Abort)"),
        0x098500: (0x13, "OTi Vendor Error 0x85 (CRC / Framing Error)"),
        0x098600: (0x0E, "OTi Vendor Error 0x86 (Peer Mode Conflict)"),
        0x098700: (0x17, "OTi Vendor Error 0x87 (FIFO Underrun)"),
        0x098800: (0x18, "OTi Vendor Error 0x88 (FIFO Overrun)"),
        0x098900: (0x19, "OTi Vendor Error 0x89 (Hardware Link Lost)"),
    }
    code, desc = mapping.get(packed, (0xFFFF, f"Unknown Sense (0x{packed:06X})"))
    return {
        "packed_sense": f"0x{packed:06X}",
        "easysuite_error_code": f"0x{code:04X}",
        "description": desc,
    }


def parse_identify_response(resp: bytes) -> dict:
    """Parse 12-byte response from 0xF0 Identify OTi-2108 (EasySuite.exe 0x00431EDF)."""
    if len(resp) < 6:
        raise ValueError(f"Identify response too short: {len(resp)} bytes")
    is_oti2108 = resp[0:5] == b"\x00\x00\x21\x08\x00"
    fw_rev = resp[5]
    fw_date_ascii = resp[6:12].decode("ascii", errors="replace") if len(resp) >= 12 else ""
    return {
        "raw_hex": resp.hex(),
        "raw_length": len(resp),
        "is_oti2108": is_oti2108,
        "chip_bcd": f"{resp[2]:02x}{resp[3]:02x}",
        "firmware_rev": fw_rev,
        "firmware_date_yymmdd": fw_date_ascii,
        "extended_cap_flag": 1 if fw_rev > 2 else 0,
    }


def parse_op1_buffer_size(resp: bytes) -> dict:
    """Parse response from 0xD8 0x00 op_type=1 (EasySuite.exe 0x004326BA / 0x0043270E)."""
    padded = resp.ljust(2, b"\x00")
    # At 0x0043270E, EasySuite.exe reads [ebp-0x1f] = resp[1] (where buffer is [ebp-0x20]);
    # fallback to resp[0] if resp[1] is 0 and resp[0] is non-zero.
    code = padded[1] if padded[1] != 0 else padded[0]
    size_map = {
        0: 0x80000,   # 512 KB default unless overridden by 0xDB 0x28 "OTiMing" at 0x0022
        1: 0x40000,   # 256 KB
        2: 0x40000,   # 256 KB
        3: 0x80000,   # 512 KB
        4: 0x80000,   # 512 KB
        5: 0x100000,  # 1 MB
        6: 0x200000,  # 2 MB
    }
    buf_bytes = size_map.get(code, 0x80000)
    return {
        "raw_hex": resp.hex(),
        "raw_length": len(resp),
        "size_code": code,
        "uses_otiming_override": code == 0,
        "buffer_size_bytes": buf_bytes,
        "buffer_size_kb": buf_bytes // 1024,
    }


def parse_op2_usb_speed(resp: bytes) -> dict:
    """Parse response from 0xD8 0x00 op_type=2 (EasySuite.exe 0x00431CE3/0x00419948 & usbnic.sys 0x000118C0)."""
    padded = resp.ljust(2, b"\x00")
    local_speed_byte = padded[0]
    peer_speed_byte = padded[1]
    local_hs = (local_speed_byte & 0x20) != 0
    if peer_speed_byte == 0x20 or local_hs:
        speed_str = "USB 2.0 High-Speed (480 Mbps)"
        link_speed_100bps = 4800000
    elif peer_speed_byte == 0x11 or local_speed_byte == 0x11:
        speed_str = "USB 1.1 Full-Speed (12 Mbps)"
        link_speed_100bps = 120000
    else:
        speed_str = f"Unknown / Idle (byte0=0x{local_speed_byte:02x}, byte1=0x{peer_speed_byte:02x})"
        link_speed_100bps = 0
    return {
        "raw_hex": resp.hex(),
        "raw_length": len(resp),
        "local_speed_byte": f"0x{local_speed_byte:02x}",
        "peer_speed_byte": f"0x{peer_speed_byte:02x}",
        "local_usb20_flag": local_hs,
        "speed_description": speed_str,
        "ndis_link_speed_100bps": link_speed_100bps,
    }


def parse_op3_poll_status(resp: bytes) -> dict:
    """
    Parse 7-byte response from 0xD8 0x00 op_type=3 (EasySuite.exe 0x0041C4AF..0x0041C4DE & usbnic.sys 0x00011640).
    Hardware returns 7 bytes (resp[0..6]):

    - resp[0]: action_code (0..7)
    - resp[1..2]: remote_queued_blocks (uint16_be, when action_code == 5)
    - resp[1..4]: virtual_disk_lba (uint32_be, assembled at [ebp-0x18..0x15] when action_code in (2, 3))
    - resp[5..6]: virtual_disk_sector_count (uint16_be, assembled at [ebp-0x20..0x1f] when action_code in (2, 3))
    """
    padded = resp.ljust(7, b"\x00")
    action_code = padded[0]
    remote_blocks = (padded[1] << 8) | padded[2]
    lba = (padded[1] << 24) | (padded[2] << 16) | (padded[3] << 8) | padded[4]
    sector_count = (padded[5] << 8) | padded[6]
    action_names = {
        0: "Idle / Link OK (0)",
        1: "Idle / Link OK (1)",
        2: "Virtual Disk Sector Read Request (0xDA 0x2A)",
        3: "Virtual Disk Sector Write Request (0xDA 0x28)",
        4: "Drive Change / Peer Mode Notification",
        5: "Remote Has Bulk Blocks Queued to Read (0xD9 0x28)",
        6: "Bridge Ready for Bulk Block Write (6 -> 0xD9 0x2A)",
        7: "Bridge Ready for Bulk Block Write (7 -> 0xD9 0x2A)",
    }
    return {
        "raw_hex": resp.hex(),
        "raw_length": len(resp),
        "action_code": action_code,
        "action_description": action_names.get(action_code, f"Unknown ({action_code})"),
        "remote_queued_blocks": remote_blocks,
        "virtual_disk_lba": lba,
        "virtual_disk_sector_count": sector_count,
    }


def parse_op5_peer_mode(resp: bytes) -> dict:
    """Parse response from 0xD8 0x00 op_type=5 (EasySuite.exe 0x00431C6B / 0x0041C6B5)."""
    padded = resp.ljust(2, b"\x00")
    mode_val = (padded[0] << 8) | padded[1]
    mode_names = {
        0x007F: "Transfer Line Mode (EasySuite.exe file sync active)",
        0x0044: "Virtual NIC Mode (usbnic.sys networking active)",
        0x0000: "Peer Offline / Uninitialized",
    }
    return {
        "raw_hex": resp.hex(),
        "raw_length": len(resp),
        "peer_mode_u16": f"0x{mode_val:04X}",
        "peer_mode_description": mode_names.get(mode_val, f"Unknown Mode (0x{mode_val:04X})"),
    }


def parse_flash_config(resp: bytes) -> dict:
    """Parse 512-byte response from 0xDB 0x28 addr=0x0022 (EasySuite.exe 0x00432789)."""
    has_otiming = resp[:7] == b"OTiMing"
    custom_size = struct.unpack_from("<I", resp, 8)[0] if len(resp) >= 12 and has_otiming else None
    return {
        "has_otiming_signature": has_otiming,
        "signature_bytes": resp[:7].decode("ascii", errors="replace"),
        "custom_buffer_size_bytes": custom_size,
    }


def exec_sg_io(fd: int, cdb: bytes, read_len: int, timeout_ms: int = 2000) -> bytes:
    """Execute a read-only SCSI CDB via Linux SG_IO ioctl."""
    cdb_buf = (ctypes.c_ubyte * len(cdb)).from_buffer_copy(cdb)
    sense_buf = (ctypes.c_ubyte * 32)()
    data_buf = (ctypes.c_ubyte * read_len)() if read_len > 0 else None

    hdr = SgIoHdr()
    hdr.interface_id = ord("S")
    hdr.dxfer_direction = SG_DXFER_FROM_DEV if read_len > 0 else SG_DXFER_NONE
    hdr.cmd_len = len(cdb)
    hdr.mx_sb_len = ctypes.sizeof(sense_buf)
    hdr.dxfer_len = read_len
    hdr.dxferp = ctypes.cast(data_buf, ctypes.c_void_p) if data_buf is not None else None
    hdr.cmdp = ctypes.cast(cdb_buf, ctypes.c_void_p)
    hdr.sbp = ctypes.cast(sense_buf, ctypes.c_void_p)
    hdr.timeout = timeout_ms

    fcntl.ioctl(fd, SG_IO, hdr)
    if hdr.status != 0 or hdr.sb_len_wr > 0:
        sb = bytes(sense_buf[: hdr.sb_len_wr])
        sk = sb[2] & 0x0F if len(sb) > 2 else 0
        asc = sb[12] if len(sb) > 12 else 0
        ascq = sb[13] if len(sb) > 13 else 0
        decoded = decode_oti_sense(sk, asc, ascq)
        raise RuntimeError(f"SCSI check condition: status={hdr.status}, sense={decoded}")
    if data_buf is None:
        return b""
    actual_len = max(0, read_len - hdr.resid) if 0 <= hdr.resid <= read_len else read_len
    return bytes(data_buf[:actual_len])


def make_libusb_executor() -> Callable[[bytes, int], bytes]:
    """Create a read-only CDB executor over direct USB Bulk-Only Transport using PyUSB."""
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise RuntimeError("PyUSB is not installed; install `pyusb` or use `--sg-dev /dev/sgX`.") from exc

    dev = usb.core.find(idVendor=USB_VID_OTI, idProduct=USB_PID_2108)
    if dev is None:
        raise RuntimeError("OTi-2108 (0ea0:2108) USB device not found.")

    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]

    ep_out = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
    )
    ep_in = usb.util.find_descriptor(
        intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
    )
    if ep_out is None or ep_in is None:
        raise RuntimeError("Could not locate Bulk IN/OUT endpoints on OTi-2108.")

    def executor(cdb: bytes, read_len: int) -> bytes:
        cbw = wrap_bot_cbw(cdb, read_len, direction_in=(read_len > 0))
        ep_out.write(cbw, timeout=2000)
        data = bytes(ep_in.read(read_len, timeout=2000)) if read_len > 0 else b""
        csw_raw = bytes(ep_in.read(13, timeout=2000))
        csw = parse_bot_csw(csw_raw)
        if not csw["status_ok"]:
            raise RuntimeError(f"BOT CSW status failed: {csw}")
        return data

    return executor


def run_probe(executor: Callable[[bytes, int], bytes]) -> dict:
    """Run the non-destructive read-only probe sequence using the provided transport executor."""
    id_resp = executor(build_cdb_identify(), 12)
    op1_resp = executor(build_cdb_read_status(1), 8)
    op2_resp = executor(build_cdb_read_status(2), 8)
    op3_resp = executor(build_cdb_read_status(3, 0), 8)
    op5_resp = executor(build_cdb_read_status(5), 8)
    flash_resp = executor(build_cdb_read_flash_config(0x0022, 1), 512)

    return {
        "identify_0xF0": parse_identify_response(id_resp),
        "status_op1_buffer_size": parse_op1_buffer_size(op1_resp),
        "status_op2_usb_speed": parse_op2_usb_speed(op2_resp),
        "status_op3_poll_state": parse_op3_poll_status(op3_resp),
        "status_op5_peer_mode": parse_op5_peer_mode(op5_resp),
        "flash_config_0xDB_0x0022": parse_flash_config(flash_resp),
    }


def run_dry_run_self_test() -> dict:
    """Validate all CDB builders, BOT CBW/CSW wrappers, sense decoders, and parsers offline."""
    # Verify CDB builders
    cdb_id = build_cdb_identify()
    assert len(cdb_id) == 16 and cdb_id[0] == 0xF0 and cdb_id[14:16] == b"OT"

    cbw = wrap_bot_cbw(cdb_id, 12, direction_in=True)
    assert len(cbw) == 31 and cbw[:4] == b"USBC" and cbw[15:31] == cdb_id

    csw_bytes = struct.pack("<IIIB", USBS_SIGNATURE, OTI_CBW_TAG, 0, 0)
    csw_parsed = parse_bot_csw(csw_bytes)
    assert csw_parsed["status_ok"] is True

    # Verify sense decoder
    s_peer = decode_oti_sense(0x09, 0x81, 0x00)
    assert s_peer["easysuite_error_code"] == "0x000B"

    # Synthetic hardware responses matching EasySuite.exe & usbnic.sys expectations
    def mock_executor(cdb: bytes, read_len: int) -> bytes:
        if cdb[0] == 0xF0:
            return bytes.fromhex("000021080003303330373239")
        if cdb[0] == 0xD8 and cdb[1] == 0x00:
            op = cdb[2]
            if op == 1:
                return b"\x00\x05"  # code 5 at resp[1] -> 1 MB buffer
            if op == 2:
                return b"\x20\x20"  # USB 2.0 High-Speed
            if op == 3:
                return b"\x05\x00\x02\x00\x00\x00\x00"  # 7-byte poll response: Action 5, 2 remote blocks queued
            if op == 5:
                return b"\x00\x7f"  # Peer in Transfer Line mode (0x007F)
        if cdb[0] == 0xDB and cdb[1] == 0x28:
            buf = bytearray(512)
            buf[:7] = b"OTiMing"
            struct.pack_into("<I", buf, 8, 0x100000)
            return bytes(buf)
        raise AssertionError(f"Unexpected CDB: {cdb.hex()}")

    report = run_probe(mock_executor)
    assert report["identify_0xF0"]["is_oti2108"] is True
    assert report["identify_0xF0"]["extended_cap_flag"] == 1
    assert report["status_op1_buffer_size"]["buffer_size_bytes"] == 0x100000
    assert report["status_op2_usb_speed"]["ndis_link_speed_100bps"] == 4800000
    assert report["status_op3_poll_state"]["action_code"] == 5
    assert report["status_op3_poll_state"]["remote_queued_blocks"] == 2
    assert report["status_op5_peer_mode"]["peer_mode_u16"] == "0x007F"
    assert report["flash_config_0xDB_0x0022"]["has_otiming_signature"] is True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Non-destructive OTi-2108 USB Host-to-Host hardware probe.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Run offline self-test with synthetic vectors.")
    group.add_argument("--sg-dev", type=str, help="Linux SG_IO device path (e.g., /dev/sg2 or /dev/sr0).")
    group.add_argument("--usb", action="store_true", help="Probe directly via libusb/PyUSB (0ea0:2108).")
    args = parser.parse_args()

    if args.dry_run:
        report = run_dry_run_self_test()
        print("[✓] OTi-2108 hardware probe dry-run self-test passed!")
        print(json.dumps(report, indent=2))
        return

    if args.sg_dev:
        fd = os.open(args.sg_dev, os.O_RDWR | os.O_NONBLOCK)
        try:
            report = run_probe(lambda cdb, rlen: exec_sg_io(fd, cdb, rlen))
        finally:
            os.close(fd)
    else:
        executor = make_libusb_executor()
        report = run_probe(executor)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
