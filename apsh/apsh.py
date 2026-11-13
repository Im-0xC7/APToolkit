import time
import usb.util
from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit

from transport import Transport

EOT   = 0x04    # device-side session-ended sentinel (apsh writes it when `sh -i` exits)
RESET = 0x12    # host->device: "kill the current shell, give me a fresh one" (sent on connect)

class _Sig(QObject):
    output = Signal(bytes)
    finished = Signal()

# TODO: combine both classes to be a single QRunnable, which will be started directly from aptoolkit

class _ShellJob(QRunnable):
    def __init__(self, t, ein, sig: _Sig):
        super().__init__()
        self.t = t
        self.ein = ein
        self.sig = sig

        self.stopped = False

    def run(self):
        if not self.t.dev:
            return

        cleared = False
        logged = False

        while not self.stopped:
            try:
                data = bytes(self.t.dev.read(self.ein, 512, timeout=1000))
                if data:
                    if EOT in data:
                        self.sig.output.emit(b"\n" + data.split(bytes([EOT]), 1)[0])
                        self.sig.output.emit("\nshell exited")
                        self.stop()
                        self.t.dev.reset()
                        return
                    self.sig.output.emit(b"\n" + data)
            except usb.core.USBError as e:
                msg = str(e).lower()
                if e.errno == 110 or "timed out" in msg or "timeout" in msg:
                    continue
                if not cleared:
                    cleared = True
                    try: self.t.dev.clear_halt(self.ein)
                    except Exception: pass
                    continue
                if not self.stopped and not logged:
                    self.sig.output.emit(f"\nIN read error: {e}")
                    logged = True
                time.sleep(0.2)

    def stop(self):
        self.stopped = True

class APShell(QObject):
    def __init__(self, parent: QObject, t: Transport, input: QLineEdit, output: QPlainTextEdit):
        super().__init__(parent)
        self.t = t
        self.input = input
        self.output = output
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.sig = _Sig()

        self.input.returnPressed.connect(self.send)
        self.sig.output.connect(self.log)

        self.job = None
        self.out = None
        self.ein = None

    def log(self, msg):
        if not isinstance(msg, str): msg = msg.decode("latin1")
        sb = self.output.verticalScrollBar()
        pos = sb.value()
        mv = pos == sb.maximum()
        self.output.setPlainText(self.output.toPlainText() + msg)
        if mv: sb.setValue(sb.maximum())
        else: sb.setValue(pos)

    def start(self):
        if not self.t.dev:
            return

        highSpeed = self.t.dev.speed == 3
        self.out = 0x03 if highSpeed else 0x02
        self.ein = 0x82 if highSpeed else 0x81

        for ep in (self.out, self.ein):
            try:
                self.t.dev.clear_halt(ep)
            except Exception as e:
                self.log(f"note: clear_halt {ep:04x}, {e}")

        self.job = _ShellJob(self.t, self.ein, self.sig)
        self.pool.start(self.job)
        self.sendOut(bytes([RESET]))

    def stop(self):
        if self.job:
            self.job.stop()
            self.job = None

    def send(self):
        msg = self.input.text()
        self.log(msg)
        self.sendOut(msg.encode() + b"\n")
        self.input.clear()

    def sendOut(self, payload, timeout=10000):
        if not self.t.dev:
            return False

        for attempt in range(5):
            try: self.t.dev.write(self.out, payload, timeout=timeout); return True
            except usb.core.USBError as e:
                try: self.t.dev.clear_halt(self.out)
                except Exception: pass
                time.sleep(0.1)
                if attempt == 4: self.log(f"OUT write error: {e}")
        return False