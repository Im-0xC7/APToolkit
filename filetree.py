import posixpath

from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
import shiboken6

PATH, ISDIR, LOADED, SELECT_NAME = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1, Qt.ItemDataRole.UserRole + 2, Qt.ItemDataRole.UserRole + 3
PROTECTED_ABS = { "/", "/user", "/user/ap-user", "/user/ap-data", "/user/images", "/user/staging", "/user/tmp", "/tmp" }

def _alive(o): return shiboken6.isValid(o)

def human(n: int):
    if n < 1024:
        return f"{n}B"
    for u in ["B", "KB", "MB"]:
        if n < 1024: return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}GB"

class _Sig(QObject):
    done = Signal(object, object)
    error = Signal(object, str)

class _ListJob(QRunnable):
    def __init__(self, t, item, path, sig):
        super().__init__()
        self.t, self.item, self.path, self.sig = t, item, path, sig
    def run(self):
        try:
            self.sig.done.emit(self.item, self.t.list_dir(self.path))
        except Exception as e:
            self.sig.error.emit(self.item, str(e))

class FileTreeController(QObject):
    selectionChanged = Signal(QTreeWidgetItem, bool, bool)

    def __init__(self, tree: QTreeWidget, transport, parent=None):
        super().__init__(parent)
        self.tree, self.t = tree, transport
        self.tree.setHeaderLabels(["Name", "Size"])
        self.pool = QThreadPool(self); self.pool.setMaxThreadCount(1)
        self.sig = _Sig()
        self.sig.done.connect(self._on_listed)
        self.sig.error.connect(self._on_error)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemSelectionChanged.connect(self._on_selection)

    def _node(self, name, path, is_dir, size=0):
        it = QTreeWidgetItem([name, "" if is_dir else human(size)])
        it.setData(0, PATH, path); it.setData(0, ISDIR, is_dir); it.setData(0, LOADED, False)
        if is_dir: it.addChild(QTreeWidgetItem(["..."]))
        return it

    def populate(self, dirs):
        self.tree.clear()
        for name, path in dirs:
            self.tree.addTopLevelItem(self._node(name, path, True))
        self.tree.topLevelItem(0).setExpanded(True)

    def refresh(self, item=None, select_name=None):
        if item is None:
            item = self.selected_item()
        if item is None or not item.data(0, ISDIR):
            return
        item.setData(0, SELECT_NAME, select_name)
        item.setData(0, LOADED, True)
        item.setExpanded(True)
        self.pool.start(_ListJob(self.t, item, item.data(0, PATH), self.sig))

    def selected_item(self):
        items = self.tree.selectedItems()
        return items[0] if items else None

    def selected_file(self):
        item = self.selected_item()
        if item is None or item.data(0, ISDIR):
            return None
        return item.data(0, PATH)

    def selected_dir(self):
        item = self.selected_item()
        if item is None or not item.data(0, ISDIR):
            return None
        return item.data(0, PATH)

    def selected_path(self):
        item = self.selected_item()
        if item is None:
            return None
        return item.data(0, PATH)

    def selected_abs(self):
        path = self.selected_path()
        if path is None:
            return None
        return posixpath.normpath(posixpath.join("/user/ap-user", path or ""))

    def is_root(self):
        item = self.selected_item()
        return item is not None and item.parent() is None

    def is_writable(self, item=None):
        if item is None:
            item = self.selected_item()
        ab = self._abs_of(item)
        return ab is not None and (ab == "/user" or ab.startswith("/user/") or ab == "/tmp" or ab.startswith("/tmp/"))

    def is_protected(self, item=None):
        if item is None:
            item = self.selected_item()
        ab = self._abs_of(item)
        return ab is not None and ab in PROTECTED_ABS

    def _abs_of(self, item):
        if item is None:
            return None
        rel = item.data(0, PATH)
        if rel is None:
            return None
        return posixpath.normpath(posixpath.join("/user/ap-user", rel or ""))

    def _on_expanded(self, item):
        if not item.data(0, ISDIR) or item.data(0, LOADED): return
        item.setData(0, LOADED, True)
        self.pool.start(_ListJob(self.t, item, item.data(0, PATH), self.sig))

    def _on_listed(self, item, entries):
        if not _alive(item): return
        item.takeChildren()
        entries.sort(key=lambda e: (not e[3], e[0].lower()))
        for name, size, mtime, is_dir, path in entries:
            item.addChild(self._node(name, path, is_dir, size))
        want = item.data(0, SELECT_NAME)
        if want is not None:
            item.setData(0, SELECT_NAME, None)
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.text(0) == want:
                    self.tree.setCurrentItem(ch)
                    self.tree.scrollToItem(ch)
                    break

    def _on_selection(self):
        items = self.tree.selectedItems()
        item = items[0] if items else None
        valid = item is not None and item.data(0, PATH) is not None
        is_dir = valid and bool(item.data(0, ISDIR))
        is_file = valid and not is_dir
        self.selectionChanged.emit(item if valid else None, is_file, is_dir)

    def _on_error(self, item, msg):
        if not _alive(item): return
        item.setData(0, LOADED, False); item.takeChildren()
        item.addChild(QTreeWidgetItem([f"[error] {msg}"]))