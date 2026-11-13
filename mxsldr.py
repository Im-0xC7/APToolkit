#!/usr/bin/env python3
"""
Pure-Python port of github.com/erstrom/mxsldr -- Freescale i.MX233/i.MX28 USB bootstream loader.

Loads a .sb bootstream into an i.MX233/28 ROM that is in USB recovery mode
over the ROM's HID "BLTC" protocol, using pyusb (libusb backend).

Use as a library:
    import mxsldr
    mxsldr.load_bootstream("recovery.sb", log=print, progress=lambda s, t: ...)

Use as a CLI:
    python3 mxsldr.py recovery.sb

Requires: pyusb  (pip install pyusb) and a libusb-1.0 runtime.
Linux: raw access to the ROM device needs root or a udev rule, e.g. /etc/udev/rules.d/99-mxs.rules
    SUBSYSTEM=="usb", ATTR{idVendor}=="15a2", ATTR{idProduct}=="004f", MODE="0666" # i.MX28
    SUBSYSTEM=="usb", ATTR{idVendor}=="066f", ATTR{idProduct}=="3780", MODE="0666" # i.MX233
"""
import struct
import sys

import usb.core
import usb.util

TRANSFER_TIMEOUT_MS = 60000

HID_SET_REPORT = 0x09
HID_REPORT_TYPE_OUTPUT = 0x02

BLTC_CMD_INQUIRY = 0x01
BLTC_CMD_DOWNLOAD_FIRMWARE = 0x02
BLTC_PAGE_INQUIRY_CHIP_INFO = 0x01

# (idVendor, idProduct, i.MX family) -- same pairs the C scans for.
DEVICES = (
    (0x066F, 0x3780, 23),   # i.MX233
    (0x15A2, 0x004F, 28),   # i.MX28
)

# HID SET_REPORT control OUT: host->device | class | recipient=interface  == 0x21
_CTRL_OUT = (usb.util.CTRL_OUT | usb.util.CTRL_TYPE_CLASS |
             usb.util.CTRL_RECIPIENT_INTERFACE)
# Interrupt IN endpoint (RECIPIENT_INTERFACE | ENDPOINT_IN in the original) == 0x81
_EP_IN = 0x81

# Report 1 payload: inquiry for the chip-info page (32 bytes, verbatim from the C).
_INQUIRY_CHIP_INFO = bytes([
    BLTC_CMD_INQUIRY, ord('B'), ord('L'), ord('T'), ord('C'), 0, 0, 0,
    0, 0x04, 0, 0,
    0, 0x80, 0, 0,
    BLTC_CMD_INQUIRY, BLTC_PAGE_INQUIRY_CHIP_INFO, 0, 0,
    0, 0, 0, 0,
    0, 0, 0, 0,
    0, 0, 0, 0,
])

# Report 1 payload: "download firmware" inquiry (32 bytes). The firmware length is patched in
# at offset 9 (little-endian u32) and offset 17 (big-endian u32), exactly as the C does.
_INQUIRY_DOWNLOAD_FW = bytearray([
    BLTC_CMD_INQUIRY, ord('B'), ord('L'), ord('T'), ord('C'), 1, 0, 0,
    0, 0x60, 0x24, 0x4A,
    0, 0, 0, 0,
    BLTC_CMD_DOWNLOAD_FIRMWARE, 0, 0x4A, 0x24,
    0x60, 0, 0, 0,
    0, 0, 0, 0,
    0, 0, 0, 0,
])


class MxsError(Exception):
    """Any failure talking to the i.MX ROM (no device, bad status, USB error)."""


def _nolog(*_a, **_k):
    pass


def find_device():
    """Return (usb.core.Device, family_int) for the first i.MX233/28 in recovery mode,
    or (None, None) if none is attached."""
    for vid, pid, fam in DEVICES:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None:
            return dev, fam
    return None, None


def _report_out(dev, report, data):
    """report 1/2 -> HID SET_REPORT control-OUT transfer. Returns bytes accepted."""
    return dev.ctrl_transfer(
        _CTRL_OUT, HID_SET_REPORT,
        (HID_REPORT_TYPE_OUTPUT << 8) | report, 0,
        bytes(data), TRANSFER_TIMEOUT_MS)


def _report_in(dev, length):
    """report 3/4 -> interrupt-IN transfer, capped at 64 bytes like the original."""
    if length > 64:
        length = 64
    return dev.read(_EP_IN, length, TRANSFER_TIMEOUT_MS)


def claim(dev):
    """Detach any kernel HID driver and claim interface 0 (mirrors the C setup)."""
    # The ROM enumerates as HID, so usbhid grabs it on Linux -> detach first.
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass  # Windows/macOS have no kernel-driver concept here
    # Need an active configuration before claiming.
    try:
        dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()
    usb.util.claim_interface(dev, 0)


def release(dev):
    try:
        usb.util.release_interface(dev, 0)
    except Exception:
        pass
    usb.util.dispose_resources(dev)


def detect(dev, log=_nolog):
    """Run the chip-info inquiry; returns a dict of chip details or raises MxsError."""
    _report_out(dev, 1, _INQUIRY_CHIP_INFO)

    buf = bytearray()
    while len(buf) < 1025:
        chunk = _report_in(dev, 1025 - len(buf))
        if not chunk:
            break
        buf += bytes(chunk)
    if len(buf) < 11:
        raise MxsError("short chip-info response (%d bytes)" % len(buf))

    fin = bytes(_report_in(dev, 14))
    if len(fin) < 5 or fin[0] != 0x04 or fin[1:5] != b"BLTS":
        raise MxsError("CPU detection failed (bad BLTS status)")

    chip_id, _pad, chip_rev, rom_ver, proto_ver = struct.unpack_from("<HBBHH", buf, 1)
    info = {
        "imx": 28 if chip_id == 0x2800 else 23,
        "chip_id": chip_id,
        "chip_rev": chip_rev,
        "rom_ver": rom_ver,
        "proto_ver": proto_ver,
    }
    log("Detected: i.MX%d" % info["imx"])
    log("  Chip ID:          0x%04x" % chip_id)
    log("  Chip Revision:    0x%04x" % chip_rev)
    log("  ROM Version:      0x%04x" % rom_ver)
    log("  Protocol Version: 0x%04x" % proto_ver)
    return info


def upload(dev, path, log=_nolog, progress=None):
    """Stream the bootstream at `path` to the ROM. `progress(sent, total)` if given."""
    with open(path, "rb") as f:
        data = f.read()
    total = len(data)
    if total <= 0:
        raise MxsError("empty bootstream: %s" % path)

    hdr = bytearray(_INQUIRY_DOWNLOAD_FW)
    struct.pack_into("<I", hdr, 9, total)    # length, little-endian
    struct.pack_into(">I", hdr, 17, total)   # length, big-endian
    _report_out(dev, 1, hdr)

    # Each report-2 packet is the report id (0x02) + up to 1024 payload bytes (buf[1025] in C).
    sent = 0
    chunk_sz = 1024
    while sent < total:
        chunk = data[sent:sent + chunk_sz]
        _report_out(dev, 2, bytes((BLTC_CMD_DOWNLOAD_FIRMWARE,)) + chunk)
        sent += len(chunk)
        if progress:
            progress(sent, total)
    log("Uploaded %d bytes (%s)" % (total, path))
    return total


def load_bootstream(path, log=_nolog, progress=None):
    """High-level one-shot: find the ROM, detect, upload `path`. Raises MxsError on failure.
    Returns the chip-info dict. Safe for a GUI worker thread (no globals)."""
    dev, fam = find_device()
    if dev is None:
        raise MxsError("No i.MX233/28 device in recovery mode found")
    try:
        claim(dev)
        info = detect(dev, log=log)
        upload(dev, path, log=log, progress=progress)
        return info
    finally:
        release(dev)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("Usage: mxsldr.py <bootstream>   (e.g. recovery.sb)\n")
        return 1
    try:
        def _bar(sent, total):
            pct = (sent * 100) // total if total else 100
            sys.stdout.write("\r  uploading: %3d%% (%d/%d)" % (pct, sent, total))
            sys.stdout.flush()
        load_bootstream(argv[1], log=print, progress=_bar)
        sys.stdout.write("\n")
        return 0
    except MxsError as e:
        sys.stderr.write("mxsldr: %s\n" % e)
        return 1
    except usb.core.USBError as e:
        sys.stderr.write("mxsldr: USB error: %s\n" % e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
