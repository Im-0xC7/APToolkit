import hashlib
import re
from pathlib import Path

import requests
from PySide6.QtCore import QObject, Signal, QRunnable
from requests import RequestException

from transport import Transport, runtask, get_stdout


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

            self.t.send(runtask(self.cmd))
            self.sig.result.emit(get_stdout(self.t.recv_until_finish()))
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
            resp = requests.get(self.url)
            self.sig.result.emit(resp.status_code, resp.text)
        except RequestException as e:
            self.sig.error.emit(e)
        finally:
            self.sig.finished.emit()

class FwDownloadSignals(QObject):
    progress = Signal(str)
    error = Signal(str)
    finished = Signal()

class FwDownloadJob(QRunnable):
    def __init__(self, model: str, version: str, outDir: str, sig: FwDownloadSignals):
        super().__init__()
        self.model = model
        self.version = version
        self.outDir = outDir
        self.sig = sig

    def run(self):
        try:
            url = f"http://www.accessecu.com/support/accessport/{self.model}/{self.version}"
            meta = requests.get(f"{url}/version_v3.txt")
            if not meta.ok:
                self.sig.error.emit(f"Requested model/version combination doesn't exist")
                return

            outDir = Path(self.outDir) / f"{self.model}_{self.version}"
            if Path.is_dir(outDir):
                self.sig.error.emit(f"Output directory {outDir} already exists")
                return

            Path.mkdir(outDir)

            for file in re.findall(r"([a-fA-F0-9]{32})\s\*(.+)", meta.text):
                self.sig.progress.emit(f"Downloading {file[1]}")
                resp = requests.get(f"http://www.accessecu.com/support/accessport/{self.model}/{self.version}/{file[1]}")
                if not resp.ok:
                    self.sig.error.emit(f"Failed to download {file[1]}!")
                    continue
                with open(f"{outDir}/{file[1]}", "wb") as f:
                    f.write(resp.content)
                with open(f"{outDir}/{file[1]}", "rb") as f:
                    if hashlib.file_digest(f, "md5").hexdigest() != file[0]:
                        self.sig.error.emit(f"Hash mismatch on {file[1]}!")

            self.sig.progress.emit(f"Finished downloading firmware to {outDir}")
        except RequestException as e:
            self.sig.error.emit(e)
        finally:
            self.sig.finished.emit()