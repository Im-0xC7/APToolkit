import hashlib
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
import usb

from PySide6.QtCore import QObject, Signal, QRunnable
from PySide6.QtWidgets import QWidget

from jobs import FwDownloadSignals, dlFirmware
from transport import Transport, ACK, VID, PID, frame, pair_archive, ENC_TAG, BOOT_TAG

IMG_QUEUE = ["ap-app.img", "ap-data.img", "ap-data2.img", "bootstream.img", "rootfs.img"]
IMG_REQUIRED = {"ap-app.img", "ap-data.img", "bootstream.img", "rootfs.img", "MD5SUMS", "version_v3.txt"}
UPDATER_META = {"manifest", "manifest2", "version_v3.txt", "MD5SUMS"}

class UpdaterSignals(QObject):
    progress = Signal(str)
    error = Signal(str)
    finished = Signal()
    askFlash = Signal()

class UpdaterJob(QRunnable):
    def __init__(self, fwDir: Path, wipeData: bool, verifyOnly: bool, t: Transport, sig: UpdaterSignals, window: QWidget):
        super().__init__()
        self.fwDir = fwDir
        self.wipeData = wipeData
        self.verifyOnly = verifyOnly
        self.t = t
        self.sig = sig
        self.window = window

        self._gate = threading.Event()
        self._reply = None

    def run(self):
        log = lambda msg: self.sig.progress.emit(msg)
        inUpdater = False
        t = self.t
        fwDir = self.fwDir
        tmp = tempfile.mkdtemp()

        def reconnect():
            tm = time.time()

            if usb.core.find(idVendor=VID, idProduct=PID):
                while time.time() - tm < 120:
                    if usb.core.find(idVendor=VID, idProduct=PID) is None: break
                    time.sleep(0.5)

            while time.time() - tm < 300:
                if usb.core.find(idVendor=VID, idProduct=PID) is not None: break
                time.sleep(1)
            else: raise RuntimeError("Device never reappeared after 300s")

            time.sleep(6)

            for _ in range(15):
                try:
                    t.open()
                    t.handshake()
                    v = t.get_version()
                    if re.match(r"[uv]\d", v or ""): return v
                except Exception: pass
                time.sleep(2)

            raise RuntimeError(f"Device reappeared but isn't responding")

        try:
            if not t.dev:
                raise RuntimeError("No device connected")

            if not fwDir.is_dir():
                raise RuntimeError("Firmware directory doesn't exist")

            if not all((fwDir / f).is_file() for f in IMG_REQUIRED):
                raise RuntimeError("Missing required file(s) in firmware directory")

            log("Starting updater download")

            dlSig = FwDownloadSignals()
            dlSig.progress.connect(self.sig.progress)
            dlSig.error.connect(self.sig.error)
            updDir = Path(dlFirmware("updater", "", tmp, dlSig))

            log("Updater download finished, verifying firmware + updater")

            for md5, name in re.findall(r"([a-fA-F0-9]{32})\s+\*?(\S+)", (fwDir / "version_v3.txt").read_text()):
                f = fwDir / name
                if not f.is_file():
                    raise RuntimeError(f"firmware/{name} is missing")
                if hashlib.md5(f.read_bytes()).hexdigest() != md5.lower():
                    raise RuntimeError(f"MD5 mismatch on firmware/{name}")

            for md5, name in re.findall(r"([a-fA-F0-9]{32})\s+\*?(\S+)", (updDir / "version_v3.txt").read_text()):
                f = updDir / name
                if not f.is_file():
                    raise RuntimeError(f"updater/{name} is missing")
                if hashlib.md5(f.read_bytes()).hexdigest() != md5.lower():
                    raise RuntimeError(f"MD5 mismatch on updater/{name}")

            curVer = t.get_version()
            newVer = f"v{(fwDir / "version_v3.txt").read_text().splitlines()[0].strip()}"
            log("Ready to start firmware update")
            log(f"Current firmware version: {curVer}")
            log(f"Target firmware version: {newVer}")

            if not self._confirm():
                log("Updater aborted")
                return

            log("Staging updater")

            files = sorted(f for f in updDir.iterdir() if f.is_file() and f.name not in UPDATER_META)

            t.handshake()
            t.runtask("show_cur no_comms", 600)
            t.send(ACK, 400)

            for d in ("../images/", "../staging/", "../tmp/"):
                try:
                    entries = t.list_dir(d)
                except Exception: continue
                df = [f for (f, _, _, is_dir, _) in entries if not is_dir]
                for f in df:
                    t.delete_file(d + f)

            for f in files:
                log(f"Staging {f.name}")
                if not t.put_enc_file(f.read_bytes(), f.name, ENC_TAG):
                    raise RuntimeError(f"Failed to stage updater file: {f.name}")

            log("Updater staged, applying manifest & rebooting into updater")

            t.apply_update((updDir / "manifest").read_bytes())
            t.close(do_eject=False)

            ver = reconnect()
            if not ver.lower().startswith("u"):
                raise RuntimeError("Device did not boot into updater, aborting")
            inUpdater = True

            sums = {}
            for l in (fwDir / "MD5SUMS").read_text().splitlines():
                if len(l.split()) < 3: continue
                sums[l.split()[1]] = (l.split()[0], int(l.split()[2]))

            log("Comparing hashes with current device firmware")

            skip = set()
            for name in IMG_QUEUE:
                if name in sums:
                    if t.check_image(name, sums[name][0], sums[name][1]):
                        log(f"Device {name} already matches new {name}")
                        skip.add(name)
            same = skip == {n for n in IMG_QUEUE if n in sums}

            if self.verifyOnly:
                if same:
                    log("All image hashes match")
                else:
                    log("One or more image hashes do not match")
                log("Verify complete, clearing update tag and rebooting")
                t.ctl_packet(frame(0x17), 1000)
                t.ctl_packet(frame(0x18), 1000)
                t.close(do_eject=False)
                return

            if same:
                log(f"All images hashes match current firmware, nothing to flash, clearing update tag and rebooting")
                t.ctl_packet(frame(0x17), 1000)
                t.ctl_packet(frame(0x18), 1000)
                t.close(do_eject=False)
                return

            t.ctl_packet(frame(0x1c, pair_archive("v1.0.0.2", "v1.1.0.0")), 400)
            t.wait_done(300)
            t.ctl_packet(frame(0x1b, pair_archive(curVer, newVer)), 400)
            t.wait_done(300)

            log("Flashing firmware, DO NOT UNPLUG")

            for name in IMG_QUEUE:
                if name not in sums: continue
                if name in skip: continue
                f = fwDir / name
                data = f.read_bytes()
                if name == "bootstream.img":
                    log("Flashing bootstream.img")
                    t.put_enc_file(data, name, BOOT_TAG)
                    t.send(frame(0x16, pair_archive(name, sums[name][0])), 3000)
                    if not t.wait_done(900):
                        raise RuntimeError(f"Bootstream flash did not complete after 900s")
                else:
                    log(f"Flashing {name}")
                    if not t.flash_nand(data, name):
                        raise RuntimeError(f"Failed to flash image: {name}")
                log(f"Verifying {name}")
                if not t.check_image(name, sums[name][0], sums[name][1]):
                    raise RuntimeError(f"Flash verify failed: {name}")

            log("Running post update script(s) then rebooting")

            t.ctl_packet(frame(0x1c, pair_archive(curVer, newVer)), 400)
            t.wait_done(600)
            t.ctl_packet(frame(0x17), 1000)
            t.ctl_packet(frame(0x18), 1000)
            t.close(do_eject=False)

            ver = reconnect()
            if ver.lower().startswith("u"):
                raise RuntimeError("Device appears to still be in updater")
            inUpdater = False

            if self.wipeData:
                log("Wiping user data and rebooting")

                log(t.runtask(
                    "about=;( ap_mount --remount=rw && cd /user && "
                    "for f in * .*; do "
                    "case \"$f\" in bootstream_md5sum|accesslink_mode|accesslink|.|..) continue ;; esac; "
                    "rm -rf -- \"$f\"; "
                    "done; "
                    "sync; ap_mount --remount=ro; echo WIPE_RC=$? )"
                ))
                t.send(frame(0x18), 1000)
                t.close(do_eject=False)

            log(f"Update complete. Current version: {ver}")
        except Exception as e:
            if inUpdater:
                for _ in range(10):
                    try:
                        t.handshake()
                        ver = (t.get_version() or "").lower()
                        if not ver.startswith("u"): break
                        t.ctl_packet(frame(0x17), 1000)
                        t.ctl_packet(frame(0x18), 1000)
                        break
                    except Exception:
                        time.sleep(2)
            self.sig.error.emit(str(e))
            self.sig.progress.emit("Updater failed")
        finally:
            shutil.rmtree(tmp)
            self.sig.finished.emit()

    def _confirm(self):
        self._gate.clear()
        self._reply = None
        self.sig.askFlash.emit()
        self._gate.wait()
        return self._reply

    def deliver(self, value):
        self._reply = value
        self._gate.set()