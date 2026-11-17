import hashlib
import re
from pathlib import Path
from urllib import request

from PySide6.QtCore import QObject, Signal, QRunnable

from transport import Transport

class RunTaskSignals(QObject):
    result = Signal(str)
    error = Signal(str)
    finished = Signal()

class RunTaskJob(QRunnable):
    def __init__(self, cmd: str, t: Transport, sig: RunTaskSignals):
        super().__init__()
        self.cmd = cmd
        self.t = t
        self.sig = sig

    def run(self):
        try:
            if not self.t.dev:
                self.sig.error.emit("No device connected")
                return

            if not self.cmd.startswith("about=;"):
                self.cmd = "about=;" + self.cmd

            self.sig.result.emit(self.t.runtask(self.cmd))
        except Exception as e:
            self.sig.error.emit(str(e))
        finally:
            self.sig.finished.emit()

class RequestSignals(QObject):
    result = Signal(int, str)
    error = Signal(str)
    finished = Signal()

class RequestJob(QRunnable):
    def __init__(self, url: str, sig: RequestSignals):
        super().__init__()
        self.url = url
        self.sig = sig

    def run(self):
        try:
            with request.urlopen(self.url) as resp:
                self.sig.result.emit(resp.status, resp.read().decode())
        except Exception as e:
            self.sig.error.emit(str(e))
        finally:
            self.sig.finished.emit()

class FwDownloadSignals(QObject):
    progress = Signal(str)
    error = Signal(str)
    finished = Signal(str)

class FwDownloadJob(QRunnable):
    def __init__(self, model: str, version: str, outDir: str, sig: FwDownloadSignals):
        super().__init__()
        self.model = model
        self.version = version
        self.outDir = outDir
        self.sig = sig

    def run(self):
        fwDir = None
        try:
            fwDir = dlFirmware(self.model, self.version, self.outDir, self.sig)

            self.sig.progress.emit(f"Finished downloading firmware to {fwDir}")
        except Exception as e:
            self.sig.error.emit(str(e))
        finally:
            self.sig.finished.emit(str(fwDir) if fwDir else None)

def dlFirmware(model: str, version: str, outDir: str, sig: FwDownloadSignals):
    url = f"http://www.accessecu.com/support/accessport/{model}{("/" + version) if version else ""}"
    meta = request.urlopen(f"{url}/version_v3.txt")
    if meta.status != 200:
        raise RuntimeError("Requested model/version combination doesn't exist")

    meta = meta.read().decode()

    if not version:
        version = meta.splitlines()[0].strip()

    outDir = Path(outDir) / f"{model}_{version}"
    if Path.is_dir(outDir):
        raise RuntimeError(f"Output directory {outDir} already exists")

    outDir.mkdir()

    for file in re.findall(r"([a-fA-F0-9]{32})\s\*(.+)", meta):
        sig.progress.emit(f"Downloading {file[1]}")
        resp = request.urlopen(f"http://www.accessecu.com/support/accessport/{model}/{version}/{file[1]}")
        if resp.status != 200:
            raise RuntimeError(f"Failed to download {file[1]}")
        with open(f"{outDir}/{file[1]}", "wb") as f:
            f.write(resp.read())
        with open(f"{outDir}/{file[1]}", "rb") as f:
            if hashlib.file_digest(f, "md5").hexdigest() != file[0]:
                raise RuntimeError(f"MD5 mismatch on {file[1]}")

    with open(f"{outDir}/version_v3.txt", "w") as f:
        f.write(meta)

    return outDir