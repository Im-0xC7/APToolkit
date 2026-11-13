import time
from pathlib import Path

import usb.util
from PySide6.QtCore import QObject, Signal, QRunnable

from transport import Transport, runtask

EOT, RESET = 0x04, 0x12

class ShellSignals(QObject):
    input = Signal(bytes)
    output = Signal(str)
    started = Signal()
    finished = Signal()

class ShellWorker(QRunnable):
    def __init__(self, t: Transport, sig: ShellSignals):
        super().__init__()
        self.t = t
        self.sig = sig

        self.out = None
        self.ein = None
        self.stopped = False

    def run(self):
        if not self.t.dev:
            return

        l = self.t.list_dir("../../tmp")
        if "apsh" not in l or "apsh.sh" not in l:
            with open(Path(__file__).resolve().parent / "apsh", "rb") as f:
                if not self.t.put_file(f.read(), "../../tmp/apsh"):
                    self.sig.output.emit("Failed to push apsh to /tmp")
                    return
            with open(Path(__file__).resolve().parent / "apsh.sh", "rb") as f:
                if not self.t.put_file(f.read(), "../../tmp/apsh.sh"):
                    self.sig.output.emit("Failed to push apsh.sh to /tmp")
                    return
        self.t.send(runtask("about=;sh /tmp/apsh.sh </dev/null >/dev/null 2>&1 &"))
        self.t.recv_until_finish()

        time.sleep(1.2)

        highSpeed = self.t.dev.speed == 3
        self.out = 0x03 if highSpeed else 0x02
        self.ein = 0x82 if highSpeed else 0x81

        self.sig.input.connect(self.write)
        self.sig.started.emit()

        for ep in (self.out, self.ein):
            try:
                self.t.dev.clear_halt(ep)
            except Exception as e:
                self.sig.output.emit(f"note: clear_halt {ep:04x}, {e}")

        self.write(bytes([RESET]))

        cleared = False
        logged = False

        while not self.stopped:
            try:
                data = bytes(self.t.dev.read(self.ein, 512, timeout=1000))
                if data:
                    if EOT in data:
                        self.sig.output.emit(f"\n{data.split(bytes([EOT]), 1)[0].decode('latin1')}")
                        self.sig.output.emit("shell exited")
                        self.stopped = True
                    else:
                        self.sig.output.emit(f"\n{data.decode('latin1')}")
            except usb.core.USBError as e:
                msg = str(e).lower()
                if e.errno == 110 or "timed out" in msg or "timeout" in msg:
                    continue
                if not cleared:
                    cleared = True
                    try:
                        self.t.dev.clear_halt(self.ein)
                    except Exception:
                        pass
                    continue
                if not self.stopped and not logged:
                    self.sig.output.emit(f"\nIN read error: {e}")
                    logged = True
                time.sleep(0.2)

        self.sig.finished.emit()

    def write(self, data, timeout=10000):
        if not self.t.dev:
            return False

        for attempt in range(5):
            try: self.t.dev.write(self.out, data, timeout=timeout); return True
            except usb.core.USBError as e:
                try: self.t.dev.clear_halt(self.out)
                except Exception: pass
                time.sleep(0.1)
                if attempt == 4: self.sig.output.emit(f"OUT write error: {e}")
        return False

    def stop(self):
        self.write(b"exit\n")