import struct
import time

import usb

VID, PID = 0x1A84, 0x0121
EP_OUT, EP_IN = 0x03, 0x82
READ_TIMEOUT_MS = 1500
PKT_TYPE_CMD = 2
MAGIC = b"serialization::archive"
BOOST_HEADER = bytes([0x03, 0x04, 0x04, 0x04, 0x08])
FINISH_MARKER = b"RUNTASK_FIN"
ACK = b"\x0a"

def frame(opcode, payload=b"", flag=0, crc=0):
    length = len(payload) + 4
    return ((bytes([PKT_TYPE_CMD])) + struct.pack(">I", length) + bytes([flag]) +
            bytes([opcode]) + payload + struct.pack(">I", crc & 0xffffffff))

def build_archive(body):
    return struct.pack("<I", len(MAGIC)) + MAGIC + BOOST_HEADER + body

def archive_string(s):
    if isinstance(s, str): s = s.encode()
    return struct.pack("<I", 1) + struct.pack("<I", len(s)) + s

def build_message(opcode, body, crc=0):
    return frame(opcode, build_archive(body), crc=crc)

def runtask(cmd):
    return build_message(0x1a, archive_string(cmd))

def get_stdout(raw: bytes) -> str:
    if not raw:
        return ""
    out = ""
    for item in parse_in(raw):
        if item[0] == "raw":
            out += item[1].decode("latin1")
        else:
            _, op, payload, s = item
            if s is not None:
                out += s.decode("latin1")
    return out + "\n"

def parse_in(stream: bytes):
    i = 0
    n = len(stream)
    while i + 7 <= n:
        if stream[i:i + 4] != struct.pack("<I", PKT_TYPE_CMD):
            # raw region until the next type-2 header (or end)
            j = stream.find(struct.pack("<I", PKT_TYPE_CMD), i + 1)
            if j < 0:
                yield "raw", stream[i:]
                return
            yield "raw", stream[i:j]
            i = j
            continue
        ln = struct.unpack_from(">I", stream, i + 1)[0] - 4
        flag = stream[i + 5] # unused here
        op = stream[i + 6]
        payload = stream[i + 7 : i + 7 + ln]
        # extract a leading string if archive-bearing
        s = None
        if payload[:4] == struct.pack("<I", len(MAGIC)) and MAGIC in payload:
            q = payload.find(MAGIC) + len(MAGIC) + len(BOOST_HEADER) + 4
            if q + 4 <= len(payload):
                sl = struct.unpack_from("<I", payload, q)[0]
                if 0 < sl < 4096 and q + 4 + sl <= len(payload):
                    s = payload[q + 4:q + 4 + sl]
        yield "msg", op, payload, s
        i = i + 7 + ln + 4

def parse_filelist(packet: bytes):
    assert packet[0] == 0x02 and packet[6] == 0x01, "not an op-1 reply"
    end = 7 + struct.unpack_from(">I", packet, 1)[0] - 4
    p = packet.find(MAGIC) + len(MAGIC) + len(BOOST_HEADER) + 6
    count = struct.unpack_from("<I", packet, p)[0]
    p += 4
    out = []
    if count:
        p += 2
        for i in range(count):
            nl = struct.unpack_from("<I", packet, p)[0]
            p += 4
            name = packet[p:p + nl].decode("latin1")
            p += nl
            meta = packet[p:p + (27 if i == 0 else 25)]
            p += len(meta)
            mtime = struct.unpack_from("<I", meta, 0)[0]
            size = struct.unpack_from("<I", meta, 4)[0]
            is_dir = bool(meta[-1])
            pl = struct.unpack_from("<I", packet, p)[0]
            p += 4
            path = packet[p:p + pl].decode("latin1")
            p += pl
            out.append((name, size, mtime, is_dir, path))
    assert p == end, f"underrun {p} != {end}"
    return out

def fileinfo_body(name, path, mtime=0, size=0):
    if isinstance(name, str): name = name.encode("latin1")
    if isinstance(path, str): path = path.encode("latin1")
    return (struct.pack("<I", 1) + b"\x00\x01" +
            struct.pack("<I", len(name)) + name +
            struct.pack("<I", mtime) + struct.pack("<I", size) +
            b"\x00" * 19 + struct.pack("<I", len(path)) + path)

def rename_body(old_name, old_path, new_name, new_path):
    old = fileinfo_body(old_name, old_path)
    if isinstance(new_name, str): new_name = new_name.encode("latin1")
    if isinstance(new_path, str): new_path = new_path.encode("latin1")
    new = struct.pack("<I", len(new_name)) + new_name + b"\x00" * 25 + struct.pack("<I", len(new_path)) + new_path
    return old + new

def packet_payload(pkt):
    return pkt[7:7 + (struct.unpack_from(">I", pkt, 1)[0] - 4)]

class Transport:
    def __init__(self):
        self.dev = None

    def open(self):
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise RuntimeError("AccessPort cobb gadget %04x:%04x not found (normal boot, connected?)" % (VID, PID))
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            dev.get_active_configuration()
        except usb.core.USBError:
            dev.set_configuration()
        usb.util.claim_interface(dev, 0)
        self.dev = dev

    def close(self, do_eject=True):
        if self.dev is not None:
            if do_eject:
                try:
                    self.send(frame(0x05))
                    self.recv(timeout=1500)
                except Exception:
                    pass
            try:
                usb.util.release_interface(self.dev, 0)
            except Exception:
                pass
            usb.util.dispose_resources(self.dev)
            self.dev = None

    def send(self, data: bytes):
        self.dev.write(EP_OUT, data, timeout=5000)

    def recv(self, maxlen=4096, timeout=READ_TIMEOUT_MS) -> bytes:
        try:
            arr = self.dev.read(EP_IN, maxlen, timeout=timeout)
            return bytes(arr)
        except usb.core.USBError:
            return b""

    def recv_packet(self, timeout=READ_TIMEOUT_MS) -> bytes:
        buf = b""
        while len(buf) < 7:
            chunk = self.recv(maxlen=65536, timeout=timeout)
            if not chunk:
                return b""
            buf += chunk
        total = 7 + struct.unpack_from(">I", buf, 1)[0]
        while len(buf) < total:
            chunk = self.recv(maxlen=65536, timeout=timeout)
            if not chunk:
                break
            buf += chunk
        return buf

    def recv_until_finish(self, total_timeout=8.0):
        buf = b""
        deadline = time.time() + total_timeout
        while time.time() < deadline:
            chunk = self.recv(timeout=800)
            if chunk:
                buf += chunk
                if FINISH_MARKER in buf:
                    break
        try:
            self.send(ACK)
            self.drain(quiet_ms=300)
        except Exception:
            pass
        return buf

    def drain(self, quiet_ms=600) -> bytes:
        buf = b""
        while True:
            chunk = self.recv(timeout=quiet_ms)
            if not chunk:
                break
            buf += chunk
        return buf

    def handshake(self):
        for op in (0x04, 0x03, 0x28, 0x1f):
            self.send(frame(op))
            self.drain()

    def list_dir(self, path=""):
        self.send(build_message(0x26, archive_string(path)))
        return parse_filelist(self.recv_packet())

    def get_file(self, path, name=None):
        if name is None:
            name = path.rsplit("/", 1)[-1]
        self.send(build_message(0x20, fileinfo_body(name, path)))
        self.recv_packet()
        self.send(build_message(0x21, archive_string(name)))
        pkt = self.recv_packet()
        if not pkt or pkt[6] != 0x01:
            raise IOError("get_file: no data reply")
        return packet_payload(pkt)

    def put_file(self, data: bytes, dest, name=None, mtime=0):
        if len(data) == 0:
            raise ValueError("put_file: data cannot be empty")
        if name is None:
            name = dest.rsplit("/", 1)[-1]
        self.send(build_message(0x22, fileinfo_body(name, dest, mtime, len(data))))
        ack = self.recv_packet()
        if not ack or packet_payload(ack)[:1] != b"\x07":
            raise IOError("put_file: setup not acked, no space or bad path")
        self.send(frame(0x23, data))
        done = self.recv_packet()
        if not done or packet_payload(done)[:1] != b"\x23":
            raise IOError("put_file: data not acked")
        return True

    def delete_file(self, path, name=None):
        if name is None:
            name = path.rsplit("/", 1)[-1]
        self.send(build_message(0x25, fileinfo_body(name, path)))
        pkt = self.recv_packet()
        return bool(pkt) and pkt[6] == 0x01

    def rename_file(self, old_path, new_path, old_name=None, new_name=None):
        if old_name is None: old_name = old_path.rsplit("/", 1)[-1]
        if new_name is None: new_name = new_path.rsplit("/", 1)[-1]
        self.send(build_message(0x24, rename_body(old_name, old_path, new_name, new_path)))
        pkt = self.recv_packet()
        return bool(pkt) and pkt[6] == 0x01