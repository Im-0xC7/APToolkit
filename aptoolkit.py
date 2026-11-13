import os
import sys
import posixpath

from PySide6.QtCore import Signal, QRunnable, QObject, QThreadPool
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QMessageBox, QHeaderView, QInputDialog
from usbmonitor import USBMonitor
from usbmonitor.attributes import ID_MODEL_ID, ID_VENDOR_ID
from datetime import datetime

from apsh.apsh import ShellWorker, ShellSignals
from transport import *
from filetree import FileTreeController

import ap_serial

from main_window import Ui_MainWindow

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
        if not self.t.dev:
            self.sig.error.emit("No device connected")
            return

        if not self.cmd.startswith("about=;"):
            self.cmd = "about=;" + self.cmd

        try:
            self.t.send(runtask(self.cmd))
            self.sig.result.emit(get_stdout(self.t.recv_until_finish()))
        except Exception as e:
            self.sig.error.emit(str(e))

        self.sig.finished.emit()

class MainWindow(QMainWindow, Ui_MainWindow):
    deviceConnected = Signal(str, object)
    deviceDisconnected = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self.pool = QThreadPool().globalInstance()
        self.pool.setMaxThreadCount(1)

        self.deviceConnected.connect(self.usbConnected)
        self.deviceDisconnected.connect(self.usbDisconnected)

        self.monitor = USBMonitor()
        self.monitor.start_monitoring(
            lambda _, info: self.deviceConnected.emit(_, info),
            lambda _, info: self.deviceDisconnected.emit(_, info),
        )

        self.t = Transport()

        # Identity
        self.readEEPROMBtn.clicked.connect(self.readEEPROM)
        self.writeEEPROMBtn.clicked.connect(self.writeEEPROM)
        self.saveEEPROMBtn.clicked.connect(self.saveEEPROM)
        self.unmarryBtn.clicked.connect(self.unmarry)
        self.updateInitSerialBtn.clicked.connect(self.updateInitSerial)
        self.rebootBtn.clicked.connect(self.reboot)

        # Files
        hdr = self.fileTreeView.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.fileTree = FileTreeController(self.fileTreeView, self.t, self)
        self.fileTree.selectionChanged.connect(self.updateFileButtons)
        self.saveBtn.clicked.connect(self.saveFile)
        self.uploadBtn.clicked.connect(self.uploadFile)
        self.createDirBtn.clicked.connect(self.createDir)
        self.renameBtn.clicked.connect(self.renameFile)
        self.deleteBtn.clicked.connect(self.deleteFile)
        self.lastDir = os.path.curdir

        # Shell
        self.startShellBtn.clicked.connect(self.startShell)
        self.stopShellBtn.clicked.connect(self.stopShell)

        self.tabWidget.setCurrentIndex(0)

        self.eeprom = None
        self.apsh = None
        self.apshSig = None

    def showEvent(self, event):
        for _, info in self.monitor.get_available_devices().items():
            self.usbConnected(_, info)

    def closeEvent(self, event):
        self.t.close()

    def log(self, text):
        self.output.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def usbConnected(self, _, info):
        if info[ID_VENDOR_ID] == f"{VID:04x}" and info[ID_MODEL_ID] == f"{PID:04x}":
            self.t.open()
            self.t.handshake()
            self.log("Accessport connected")
            self.fileTree.populate([("/", "../..")])

    def usbDisconnected(self, _, info):
        if info[ID_VENDOR_ID] == f"{VID:04x}" and info[ID_MODEL_ID] == f"{PID:04x}":
            self.t.close(False)
            self.fileTreeView.clear()
            self.updateFileButtons(None, False, False)
            self.log("Accessport disconnected")

    def _onEEPROMRead(self, out: str):
        self.eeprom = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        self.updateEEPROMFields()
        self.log("EEPROM read successfully")

    def _onEEPROMReadFinished(self):
        self.readEEPROMBtn.setEnabled(True)

    def readEEPROM(self):
        self.readEEPROMBtn.setEnabled(False)

        sig = RunTaskSignals()
        sig.error.connect(self.log)
        sig.result.connect(self._onEEPROMRead)
        sig.finished.connect(self._onEEPROMReadFinished)
        self.pool.start(RunTaskJob("(readeeprom; if [ -e /user/ap-data/user_vin ]; then VIN=$(cat /user/ap-data/user_vin); else VIN=Unknown; fi; echo VIN=$VIN)", self.t, sig))

    def _onEEPROMWrite(self, out: str):
        if "RC=0" in out:
            self.log("EEPROM written successfully")
        else:
            self.log("EEPROM write unsuccessful, check fields and try again")

    def _onEEPROMWriteFinished(self):
        self.writeEEPROMBtn.setEnabled(True)

    def writeEEPROM(self):
        if not self.validateEEPROMFields():
            self.log("Check the highlighted field and try again")
            return

        reply = QMessageBox.question(self, "Confirm EEPROM Write",
                                     "Are you sure you want to proceed?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        cmd = (
            "about=;writeeeprom --quiet "
            f"-m {self.eeprom["AP_MANUF"]} "
            f"-r {self.eeprom["AP_REGION"]} "
            f"-v {self.eeprom["AP_MODEL"].lstrip("0") or "0"} "
            f"-C {self.eeprom["AP_VENDOR"]} "
            f"-n {self.eeprom["AP_SERIAL"]} "
            f"-g {self.eeprom["AP_UPGR_SERIAL"]} "
            f"-s {self.eeprom["AP_STATE"]} "
            f"-u {self.eeprom["AP_CM1_STATE"]} "
            f"-k {self.eeprom["AP_LOCK"]} "
            f"-p {self.eeprom["AP_CM1_LOCK"]} "
            f"-i {self.eeprom["AP_VEHICLE"]} "
            f"-j {self.eeprom["AP_CM1_VEHICLE"]} "
            f"-{"a" if self.eeprom["AP_FEATURE_TRANS_TUNING"] == 1 else "d"} AP_FEATURE_TRANS_TUNING; "
            f"echo RC=$?"
        )

        self.writeEEPROMBtn.setEnabled(False)

        sig = RunTaskSignals()
        sig.error.connect(self.log)
        sig.result.connect(self._onEEPROMWrite)
        sig.finished.connect(self._onEEPROMWriteFinished)
        self.pool.start(RunTaskJob(cmd, self.t, sig))

    def saveEEPROM(self):
        if self.eeprom:
            path, _ = QFileDialog.getSaveFileName(self, "Save EEPROM File", "eeprom.txt", "Text Files (*.txt);;All Files (*)")
            if path:
                with open(path, "w") as f:
                    f.write("\n".join(f"{k}={v}" for k, v in self.eeprom.items()))
                self.log(f"EEPROM saved to {path}")
        else:
            self.log("No EEPROM loaded")

    def _onUnmarry(self, out: str):
        if not "UNMARRY_RC=0" in out:
            self.log(f"Unmarry failed! Response:\n{out}")
            return

        self.log("Unmarry succeeded, rebooting")
        self.reboot()

    def _onUnmarryFinished(self):
        self.unmarryBtn.setEnabled(True)

    def unmarry(self):
        if not self.t.dev:
            self.log("No device connected")
            return

        reply = QMessageBox.question(self, "Confirm Unmarry",
                                     "Are you sure you want to proceed?\nThis action is irreversible!",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        cmd = (
            'about=;(set -f; '
            'S=$(readeeprom -s); '
            'case "$S" in '
            '*AP_CM1_STATE=1*|*AP_CM1_STATE=2*) CM1="--cm1_state=0 --cm1_vehicle=?";; '
            '*) CM1="";; '
            'esac; '
            'ap_mount --remount=rw; '
            'writeeeprom --quiet --state=0 --vehicle=? $CM1; RC=$?; '
            'if [ $RC -eq 0 ]; then '
            'rm -f '
            '/user/ap-data/user.rom /user/ap-data/user.md5sum '
            '/user/ap-data/user_tcm.rom /user/ap-data/user_tcm.md5sum '
            '/user/ap-data/user_df0.rom /user/ap-data/user_df1.rom '
            '/user/ap-data/vidblock /user/ap-data/flashed_rom '
            '/user/ap-data/user_vin /user/ap-data/ecu_og_id /user/ap-data/tcm_og_id '
            '/user/ap-data/original_ecu.txt /user/ap-data/did_dump.txt '
            '/user/ap-data/preinstall_stored_data /user/ap-data/recovery_settings '
            '/user/ap-data/prev_install /user/ap-user/backupcksum; '
            'cur_map --working=/user/ap-data --clear --realtime --reflash --quiet; '
            'if [ -n "$CM1" ]; then cur_map --working=/user/ap-data --clear --tcm_reflash --quiet; fi; '
            'fi; '
            'ap_mount --remount=ro; '
            'echo UNMARRY_RC=$RC; '
            'readeeprom -s -i -k -f)'
        )

        self.unmarryBtn.setEnabled(False)

        sig = RunTaskSignals()
        sig.error.connect(self.log)
        sig.result.connect(self._onUnmarry)
        sig.finished.connect(self._onUnmarryFinished)
        self.pool.start(RunTaskJob(cmd, self.t, sig))

    def _onUpdateInitSerial(self, out: str):
        if "RC=0" in out:
            self.log("Original serial number updated")
            self.readEEPROM()
        else:
            self.log("Failed to update original serial number")

    def _onUpdateInitSerialFinished(self):
        self.updateInitSerialBtn.setEnabled(True)

    def updateInitSerial(self):
        reply = QMessageBox.question(self, "Confirm Update",
                                     "Are you sure you want to update the original serial number?\n(Don't do this unless you know what you're doing)",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.updateInitSerialBtn.setEnabled(False)

        sig = RunTaskSignals()
        sig.error.connect(self.log)
        sig.result.connect(self._onUpdateInitSerial)
        sig.finished.connect(self._onUpdateInitSerialFinished)
        self.pool.start(RunTaskJob("writeeeprom --quiet -I;echo RC=$?", self.t, sig))

    def reboot(self):
        if self.t.dev:
            self.t.send(frame(0x18))
            self.t.drain()
        else:
            self.log("No device connected")

    def updateEEPROMFields(self):
        self.manuf.setCurrentIndex(self.manuf.findText(self.eeprom["AP_MANUF"]))
        self.region.setCurrentIndex(self.region.findText(self.eeprom["AP_REGION"]))
        self.model.setText(self.eeprom["AP_MODEL"])
        self.vendor.setText(int(self.eeprom["AP_VENDOR"]).to_bytes(4, "big").decode("ascii"))
        self.partNum.setText(self.eeprom["AP_PART_NUM"])
        self.serial.setText(self.eeprom["AP_SERIAL"])
        self.initSerial.setText(self.eeprom["AP_INIT_SERIAL"])
        self.upgrSerial.setText(self.eeprom["AP_UPGR_SERIAL"])
        self.userSerial.setText(self.eeprom["AP_USER_SERIAL"])
        self.serialSticker.setText(self.eeprom["AP_SERIAL_STICKER"])
        self.state.setCurrentIndex(int(self.eeprom["AP_STATE"]) + 1)
        self.lock.setCurrentIndex(int(self.eeprom["AP_LOCK"]) + 1)
        self.cm1State.setCurrentIndex(int(self.eeprom["AP_CM1_STATE"]) + 1)
        self.cm1Lock.setCurrentIndex(int(self.eeprom["AP_CM1_LOCK"]) + 1)
        self.tcmTuning.setCurrentIndex(int(self.eeprom["AP_FEATURE_TRANS_TUNING"]))
        self.vehicle.setText(self.eeprom["AP_VEHICLE"])
        self.cm1Vehicle.setText(self.eeprom["AP_CM1_VEHICLE"])
        self.vin.setText(self.eeprom["VIN"])

    def validateEEPROMFields(self):
        # MANUF
        if self.manuf.currentIndex() > -1:
            self.eeprom["AP_MANUF"] = self.manuf.currentText()
        else:
            self.manuf.setFocus()
            return False

        # REGION
        if self.region.currentIndex() > -1:
            self.eeprom["AP_REGION"] = self.region.currentText()
        else:
            self.region.setFocus()
            return False

        # MODEL
        try:
            if 0 < int(self.model.text()) < 255:
                self.eeprom["AP_MODEL"] = self.model.text()
            else:
                self.model.selectAll()
                return False
        except (ValueError, TypeError):
            self.model.selectAll()
            return False

        # VENDOR
        if len(self.vendor.text()) == 4 and self.vendor.text().isascii():
            self.eeprom["AP_VENDOR"] = self.vendor.text()
        else:
            self.vendor.selectAll()
            return False

        # SERIAL
        ok, reason, fields = ap_serial.validate(self.serial.text())
        if ok \
        and ap_serial.MANUF[fields["manuf"]] == self.manuf.currentText() \
        and ap_serial.REGION[fields["region"]] == self.region.currentText() \
        and fields["part"] == int(self.model.text()):
            self.eeprom["AP_SERIAL"] = self.serial.text()
        else:
            self.serial.selectAll()
            return False

        # UPGR_SERIAL
        try:
            if 0 <= int(self.upgrSerial.text(), 0) <= 0xFFFFFFFF:
                self.eeprom["AP_UPGR_SERIAL"] = self.upgrSerial.text()
            else:
                self.upgrSerial.selectAll()
                return False
        except (ValueError, TypeError):
            self.upgrSerial.selectAll()
            return False

        # STATE
        if self.state.currentIndex() > -1:
            self.eeprom["AP_STATE"] = str(self.state.currentIndex() - 1)
        else:
            self.state.setFocus()
            return False

        # LOCK
        if self.lock.currentIndex() > -1:
            self.eeprom["AP_LOCK"] = str(self.lock.currentIndex() - 1)
        else:
            self.lock.setFocus()
            return False

        # CM1_STATE
        if (self.cm1State.currentIndex() == 0 and self.tcmTuning.currentIndex() == 0) or (self.cm1State.currentIndex() > 0 and self.tcmTuning.currentIndex() == 1):
            self.eeprom["AP_CM1_STATE"] = str(self.cm1State.currentIndex() - 1)
        else:
            self.cm1State.setFocus()
            return False

        # CM1_LOCK
        if (self.cm1Lock.currentIndex() == 0 and self.tcmTuning.currentIndex() == 0) or (self.cm1Lock.currentIndex() > 0 and self.tcmTuning.currentIndex() == 1):
            self.eeprom["AP_CM1_LOCK"] = str(self.cm1Lock.currentIndex() - 1)
        else:
            self.cm1Lock.setFocus()
            return False

        # FEATURE_TRANS_TUNING
        if self.tcmTuning.currentIndex() > -1:
            self.eeprom["AP_FEATURE_TRANS_TUNING"] = self.tcmTuning.currentIndex()
        else:
            self.tcmTuning.setFocus()
            return False

        # VEHICLE
        if (self.state.currentIndex() == 1 and self.vehicle.text() == "?") or self.state.currentIndex() != 1:
            self.eeprom["AP_VEHICLE"] = self.vehicle.text()
        else:
            self.vehicle.selectAll()
            return False

        # CM1_VEHICLE
        if (self.cm1State.currentIndex() == 1 and self.cm1Vehicle.text() == "?") or self.cm1State.currentIndex() != 1:
            self.eeprom["AP_CM1_VEHICLE"] = self.cm1Vehicle.text()
        else:
            self.cm1Vehicle.selectAll()
            return False

        return True

    def saveFile(self):
        path = self.fileTree.selected_file()
        if path is None:
            return
        name = path.rsplit("/", 1)[-1]
        local, _ = QFileDialog.getSaveFileName(self, "Save File", os.path.join(self.lastDir, name))
        if not local:
            return
        self.lastDir = os.path.dirname(local)
        try:
            data = self.t.get_file(path, name)
            with open(local, "wb") as f:
                f.write(data)
            self.log(f"File saved to {local}")
        except Exception as e:
            self.log(f"Download failed: {e}")

    def uploadFile(self):
        path = self.fileTree.selected_dir()
        if path is None:
            return
        local, _ = QFileDialog.getOpenFileName(self, "Upload File", os.path.join(self.lastDir, path))
        if not local:
            return
        self.lastDir = os.path.dirname(local)
        name = os.path.basename(local)
        dest = posixpath.join(path, name)
        absPath = posixpath.join(self.fileTree.selected_abs(), name)
        try:
            with open(local, "rb") as f:
                self.t.put_file(f.read(), dest)
            self.log(f"File uploaded to {absPath}")
            self.fileTree.refresh(self.fileTree.selected_item(), select_name=name)
        except Exception as e:
            self.log(f"Upload failed: {e}")

    def createDir(self):
        path = self.fileTree.selected_path()
        if path is None:
            return
        name, ok = QInputDialog.getText(self, "Create Directory", "Directory name:")
        if not name or not ok:
            return
        dest = posixpath.join(path, name)
        temp = posixpath.join(dest, ".temp")
        absPath = posixpath.join(self.fileTree.selected_abs(), name)
        if not self.t.put_file(b"\x00", temp):
            self.log(f"Failed to create directory {absPath} (read-only?)")
            return
        if not self.t.delete_file(temp):
            self.log("Failed to delete temporary file after creating directory")
            return
        self.log(f"Created directory {absPath}")
        self.fileTree.refresh(self.fileTree.selected_item(), select_name=name)

    def renameFile(self):
        path = self.fileTree.selected_path()
        if path is None:
            return
        name, ok = QInputDialog.getText(self, "Rename File", "New name:", text=os.path.basename(path))
        if not ok or not name or "/" in name:
            return
        newPath = posixpath.join(str(posixpath.dirname(path)), name)
        if self.t.rename_file(path, newPath):
            self.log(f"Renamed {self.fileTree.selected_abs()} to {posixpath.normpath(posixpath.join("/user/ap-user", newPath))}")
            self.fileTree.refresh(self.fileTree.selected_item().parent())
        else:
            self.log(f"Failed to rename {path} (read-only?)")

    def deleteFile(self):
        if self.fileTree.is_root():
            return
        path = self.fileTree.selected_path()
        if path is None:
            return
        absPath = self.fileTree.selected_abs()
        reply = QMessageBox.question(self, "Confirm Delete",
                                     "Are you sure you want to proceed?\nThis action is irreversible!",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        if not self.t.delete_file(path):
            self.log(f"Failed to delete {absPath} (read-only or non-empty?)")
            return
        self.log(f"Deleted {absPath}")
        self.fileTree.refresh(self.fileTree.selected_item().parent())

    def updateFileButtons(self, item, isFile, isDir):
        writable = self.fileTree.is_writable(item)
        protected = self.fileTree.is_protected(item)
        self.saveBtn.setEnabled(isFile)
        self.uploadBtn.setEnabled(isDir and writable)
        self.createDirBtn.setEnabled(isDir and writable)
        self.renameBtn.setEnabled((isFile or isDir) and writable and not protected)
        self.deleteBtn.setEnabled((isFile or isDir) and writable and not protected)

    def _onShellInput(self):
        msg = self.shellInput.text()
        self._onShellOutput(msg)
        self.apshSig.input.emit(b"%s\n" % msg.encode())
        self.shellInput.clear()

    def _onShellOutput(self, data: str):
        sb = self.shellOutput.verticalScrollBar()
        pos = sb.value()
        mv = pos == sb.maximum()
        self.shellOutput.setPlainText(self.shellOutput.toPlainText() + data)
        if mv: sb.setValue(sb.maximum())
        else: sb.setValue(pos)

    def _onShellStarted(self):
        self.stopShellBtn.setEnabled(True)

    def _onShellFinished(self):
        self.startShellBtn.setEnabled(True)
        self.stopShellBtn.setEnabled(False)

    def startShell(self):
        self.apshSig = ShellSignals()
        self.shellInput.returnPressed.connect(self._onShellInput)
        self.apshSig.output.connect(self._onShellOutput)
        self.apshSig.started.connect(self._onShellStarted)
        self.apshSig.finished.connect(self._onShellFinished)
        self.apsh = ShellWorker(self.t, self.apshSig)

        self.startShellBtn.setEnabled(False)
        self.pool.start(self.apsh)

    def stopShell(self):
        if self.apsh:
            self.apsh.stop()
            self.startShellBtn.setEnabled(True)
            self.stopShellBtn.setEnabled(False)
            self.shellInput.returnPressed.disconnect()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())