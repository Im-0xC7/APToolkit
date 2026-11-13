"""
Validate / decompose an AccessPort v3 AP_SERIAL.

Mirrors ap-app/bin/serialnumber.sh (FW 1.8.0.0):
  AP_SERIAL = HW<<29 | PART<<24(&0x1F) | MANUF<<20(&0xF) | REGION<<17(&0x7) | SN(&0x1FFFF)

Structural validity == is_v3_serial(): HW==3 and PART!=0 and MANUF!=0 and SN!=0.
Region is deliberately NOT checked ("No way to validate a region! Skip it!").
"""

# --- field layout (serialnumber.sh:11-18) ---
HW_SHIFT, PN_SHIFT, MF_SHIFT, RC_SHIFT = 29, 24, 20, 17
PN_MASK, MF_MASK, RC_MASK, SN_MASK = 0x1F, 0xF, 0x7, 0x1FFFF
V3_HW = 0x3

# manuf_enum_to_str (serialnumber.sh:142); note 5/13/14 are intentionally absent
MANUF = {1: "SUB", 2: "MIT", 3: "NIS", 4: "MAZ", 6: "MIN", 7: "BMW",
         8: "FOR", 9: "POR", 10: "VLK", 11: "HON", 12: "TOY", 15: "APA"}
REGION = {0: "US", 4: "AU"}  # only these two are ever assigned


def _coerce(s):
    """Accept int, decimal str, or 0x-hex str; mask to unsigned 32-bit."""
    if isinstance(s, str):
        s = s.strip()
        v = int(s, 16) if s.lower().startswith("0x") else int(s, 10)
    else:
        v = int(s)
    return v & 0xFFFFFFFF


def unpack(serial):
    v = _coerce(serial)
    return {
        "packed":    v,
        "hw":        v >> HW_SHIFT,
        "part":      (v >> PN_SHIFT) & PN_MASK,
        "manuf":     (v >> MF_SHIFT) & MF_MASK,
        "region":    (v >> RC_SHIFT) & RC_MASK,
        "serial":    v & SN_MASK,
        "manuf_str": MANUF.get((v >> MF_SHIFT) & MF_MASK, ""),
    }


def is_v3_serial(serial):
    """Device-exact structural check (serialnumber.sh::is_v3_serial)."""
    f = unpack(serial)
    return (f["hw"] == V3_HW and f["part"] != 0
            and f["manuf"] != 0 and f["serial"] != 0)


def validate(serial, strict=False):
    """
    Returns (ok, reason, fields).
    strict=True additionally requires MANUF to be a known enum value
    (rejects e.g. 5/13/14 that pass the device's bare !=0 test but map to "").
    """
    try:
        f = unpack(serial)
    except (ValueError, TypeError) as e:
        return (False, f"unparseable: {e}", None)
    if f["hw"] != V3_HW:
        return (False, f"HW nibble {f['hw']} != 3 (not a v3 serial)", f)
    if f["part"] == 0:
        return (False, "part number is 0", f)
    if f["manuf"] == 0:
        return (False, "manuf enum is 0", f)
    if f["serial"] == 0:
        return (False, "serial number is 0", f)
    if strict and f["manuf"] not in MANUF:
        return (False, f"manuf enum {f['manuf']} not a known manufacturer", f)
    return (True, "ok", f)


def build_sticker(serial):
    """Human sticker, mirrors print_v3_serial: MMM + PP + SSSSS (e.g. FOR0675606).
    SUB + non-US regionizes the part digits (AU -> part+10)."""
    f = unpack(serial)
    part = f["part"]
    if f["manuf_str"] == "SUB" and f["region"] == 4:      # AU
        part = 10 + f["part"]
    if f["manuf_str"] == "SUB" and part > 31:
        part = 0
    return f"{f['manuf_str']:.3}{part:02d}{f['serial']:05d}"


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        ok, why, f = validate(arg, strict=True)
        tag = "VALID  " if ok else "INVALID"
        if f:
            print(f"{tag} {arg}: {why} | hw={f['hw']} part={f['part']} "
                  f"manuf={f['manuf']}({f['manuf_str']}) region={f['region']}"
                  f"({REGION.get(f['region'],'?')}) sn={f['serial']} "
                  f"sticker={build_sticker(f['packed'])}")
        else:
            print(f"{tag} {arg}: {why}")