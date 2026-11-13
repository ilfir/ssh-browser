from __future__ import annotations

import asyncio
import os
import posixpath
import tempfile
from datetime import datetime
from pathlib import Path

import asyncssh
from PySide6.QtCore import QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog, QFileIconProvider,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QToolBar, QTreeView, QVBoxLayout, QWidget,
                               QHeaderView, QLineEdit)

from .config import ProfileStore
from .hosts_dialog import HostsDialog
from .sftp_service import (HostKeyUnknown, RemoteEntry, RemoteError, RemoteSession, trust_host_key)
from .transfers import Transfer, TransferManager

SIZE_ROLE = Qt.ItemDataRole.UserRole + 1
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 2
OPEN_LIMIT = 100 * 1024 * 1024


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} B" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return ""


class _Sorter(QSortFilterProxyModel):
    """Folders always first, then by the clicked column's sort key."""

    def lessThan(self, a, b):
        ea = self.sourceModel().item(a.row(), 0).data(ENTRY_ROLE)
        eb = self.sourceModel().item(b.row(), 0).data(ENTRY_ROLE)
        asc = self.sortOrder() == Qt.SortOrder.AscendingOrder
        if ea.is_dir != eb.is_dir:
            return ea.is_dir == asc  # keeps dirs on top for both directions
        va, vb = a.data(SIZE_ROLE), b.data(SIZE_ROLE)
        return (va or 0) < (vb or 0) if isinstance(va, (int, float)) else str(a.data()).casefold() < str(b.data()).casefold()


class _FileView(QTreeView):
    def __init__(self, on_drop):
        super().__init__()
        self._on_drop = on_drop
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        idx = self.indexAt(e.position().toPoint())
        self._on_drop(paths, idx)
        e.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self, store: ProfileStore):
        super().__init__()
        self.setWindowTitle("SSH Browser")
        self.resize(1100, 700)
        self.store = store
        self.session: RemoteSession | None = None
        self._password: str | None = None
        self.cwd = ""
        self.entries: list[RemoteEntry] = []
        self.back: list[str] = []
        self.fwd: list[str] = []
        self.clip: tuple[str, list[str]] = ("copy", [])
        self.show_hidden = False
        self.transfers = TransferManager()
        self.transfers.changed.connect(self._transfer_changed)
        self.transfers.finished.connect(self._transfer_finished)
        self._rows: dict[int, int] = {}
        self._icons = QFileIconProvider()
        self._build_ui()
        self._set_connected(False)

    # ================= UI =================
    def _act(self, text, slot, shortcut=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(lambda *_: slot())
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
        return a

    def _build_ui(self):
        self.host_box = QComboBox(); self.host_box.setMinimumWidth(180)
        self.connect_act = self._act("Connect", self.toggle_connection)
        self.hosts_act = self._act("Hosts…", self.manage_hosts)
        tb = QToolBar("Connection"); tb.setMovable(False)
        tb.addWidget(self.host_box); tb.addAction(self.connect_act); tb.addAction(self.hosts_act)
        self.addToolBar(tb)

        self.a_back = self._act("◀", self.go_back, "Ctrl+[", "Back")
        self.a_fwd = self._act("▶", self.go_fwd, "Ctrl+]", "Forward")
        self.a_up = self._act("▲", self.go_up, "Ctrl+Up", "Enclosing folder")
        self.a_refresh = self._act("⟳", self.refresh, "Ctrl+R", "Refresh")
        self.a_upload = self._act("Upload…", self.upload_dialog, "Ctrl+U")
        self.a_download = self._act("Download…", self.download_selected, "Ctrl+D")
        self.a_mkdir = self._act("New Folder", self.new_folder, "Ctrl+Shift+N")
        self.a_rename = self._act("Rename", self.rename_selected, "Return")
        self.a_delete = self._act("Delete", self.delete_selected, "Ctrl+Backspace")
        self.a_copy = self._act("Copy", lambda: self.set_clip("copy"), "Ctrl+C")
        self.a_cut = self._act("Cut", lambda: self.set_clip("cut"), "Ctrl+X")
        self.a_paste = self._act("Paste", self.paste, "Ctrl+V")
        self.a_hidden = self._act("Show hidden", self.toggle_hidden, "Ctrl+Shift+.")
        self.a_hidden.setCheckable(True)
        self.a_open = self._act("Open", self.open_selected)
        self.a_size = self._act("Calculate size", self.calc_size)

        nav = QToolBar("Navigate"); nav.setMovable(False)
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_refresh):
            nav.addAction(a)
        self.path_edit = QLineEdit(); self.path_edit.returnPressed.connect(self.goto_typed)
        nav.addWidget(self.path_edit)
        for a in (self.a_upload, self.a_download, self.a_mkdir, self.a_delete, self.a_hidden):
            nav.addAction(a)
        self.addToolBar(nav)
        self.nav_actions = [self.a_back, self.a_fwd, self.a_up, self.a_refresh, self.a_upload, self.a_download,
                            self.a_mkdir, self.a_rename, self.a_delete, self.a_copy, self.a_cut, self.a_paste,
                            self.a_hidden, self.a_open, self.a_size]
        for a in self.nav_actions:
            self.addAction(a)  # makes shortcuts work window-wide

        self.model = QStandardItemModel(0, 4)
        self.model.setHorizontalHeaderLabels(["Name", "Size", "Modified", "Permissions"])
        self.proxy = _Sorter(); self.proxy.setSourceModel(self.model)
        self.view = _FileView(self._dropped)
        self.view.setModel(self.proxy)
        self.view.setRootIsDecorated(False); self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.header().setStretchLastSection(False)
        self.view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, w in ((1, 90), (2, 150), (3, 110)):
            self.view.header().setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
            self.view.setColumnWidth(c, w)
        self.view.doubleClicked.connect(lambda i: self.open_selected())
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)

        self.tt = QTableWidget(0, 4)
        self.tt.setHorizontalHeaderLabels(["Transfer", "Item", "Progress", "Status"])
        self.tt.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tt.setColumnWidth(0, 90); self.tt.setColumnWidth(2, 220); self.tt.setColumnWidth(3, 160)
        self.tt.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tt.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        cancel = QPushButton("Cancel selected"); cancel.clicked.connect(self.cancel_selected_transfer)
        clear = QPushButton("Clear finished"); clear.clicked.connect(self.clear_transfers)
        bar = QHBoxLayout(); bar.addWidget(QLabel("Transfers")); bar.addStretch(); bar.addWidget(cancel); bar.addWidget(clear)
        bottom = QWidget(); bl = QVBoxLayout(bottom); bl.setContentsMargins(4, 2, 4, 2); bl.addLayout(bar); bl.addWidget(self.tt)

        split = QSplitter(Qt.Orientation.Vertical); split.addWidget(self.view); split.addWidget(bottom)
        split.setStretchFactor(0, 4); split.setStretchFactor(1, 1)
        self.setCentralWidget(split)
        self.statusBar().showMessage("Not connected")
        self._reload_hosts()

    def _reload_hosts(self, select_id: str | None = None):
        self.host_box.clear()
        for p in self.store.profiles:
            self.host_box.addItem(p.name, p.id)
        if select_id:
            i = self.host_box.findData(select_id)
            if i >= 0:
                self.host_box.setCurrentIndex(i)

    def _set_connected(self, on: bool):
        for a in self.nav_actions:
            a.setEnabled(on)
        self.view.setEnabled(on); self.path_edit.setEnabled(on)
        self.connect_act.setText("Disconnect" if on else "Connect")
        self.host_box.setEnabled(not on)

    # ================= helpers =================
    def spawn(self, coro, done=None):
        async def run():
            try:
                res = await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._error(e)
                return
            if done:
                done(res)
        return asyncio.ensure_future(run())

    def _error(self, e: Exception):
        if isinstance(e, (asyncssh.ConnectionLost, asyncssh.DisconnectError, BrokenPipeError, ConnectionResetError)):
            self.statusBar().showMessage("Connection lost")
            self.session = None
            self._set_connected(False)
            QMessageBox.warning(self, "Disconnected", "The connection to the remote machine was lost. Connect again to continue.")
            return
        msg = str(e) or type(e).__name__
        self.statusBar().showMessage(msg, 8000)
        QMessageBox.warning(self, "Error", msg)

    def selected(self) -> list[RemoteEntry]:
        out = []
        for idx in self.view.selectionModel().selectedRows(0):
            out.append(self.model.item(self.proxy.mapToSource(idx).row(), 0).data(ENTRY_ROLE))
        return out

    # ================= connection =================
    def manage_hosts(self):
        HostsDialog(self.store, self).exec()
        self._reload_hosts()

    def toggle_connection(self):
        if self.session:
            s, self.session = self.session, None
            asyncio.ensure_future(s.close())
            self._set_connected(False)
            self.model.removeRows(0, self.model.rowCount())
            self.path_edit.clear()
            self.statusBar().showMessage("Disconnected")
            return
        pid = self.host_box.currentData()
        profile = next((p for p in self.store.profiles if p.id == pid), None)
        if not profile:
            self.manage_hosts()
            return
        self.spawn(self._connect(profile))

    async def _connect(self, profile, password: str | None = None, retried: bool = False):
        self.statusBar().showMessage(f"Connecting to {profile.name}…")
        password = password if password is not None else profile.get_password()
        try:
            session = await RemoteSession.connect(profile, password)
        except HostKeyUnknown as e:
            ok = QMessageBox.question(
                self, "Unknown host",
                f"The authenticity of {e.host}:{e.port} can't be established.\n\nFingerprint:\n{e.fingerprint}\n\n"
                "Trust this host and continue?") == QMessageBox.StandardButton.Yes
            if not ok:
                self.statusBar().showMessage("Cancelled")
                return
            trust_host_key(e.host, e.port, e.key)
            return await self._connect(profile, password, retried)
        except asyncssh.PermissionDenied:
            if retried:
                self._error(RemoteError("Authentication failed."))
                return
            pw, ok = QInputDialog.getText(self, "Password", f"Password for {profile.username or 'user'}@{profile.host}:",
                                          QLineEdit.EchoMode.Password)
            if not ok:
                self.statusBar().showMessage("Cancelled")
                return
            return await self._connect(profile, pw, True)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage("Connection failed")
            self._error(e)
            return
        self.session, self._password = session, password
        self._set_connected(True)
        self.back.clear(); self.fwd.clear()
        await self._navigate(await session.home(), push=False)
        self.statusBar().showMessage(f"Connected to {profile.name}")

    # ================= navigation =================
    async def _navigate(self, path: str, push: bool = True):
        if not self.session:
            return
        try:
            path = await self.session.realpath(path)
            entries = await self.session.listdir(path)
        except Exception as e:  # noqa: BLE001
            self._error(e)
            return
        if push and self.cwd and self.cwd != path:
            self.back.append(self.cwd); self.fwd.clear()
        self.cwd, self.entries = path, entries
        self.path_edit.setText(path)
        self.setWindowTitle(f"{posixpath.basename(path) or '/'} — SSH Browser")
        self._render()

    def _render(self):
        self.model.removeRows(0, self.model.rowCount())
        shown = 0
        for e in self.entries:
            if not self.show_hidden and e.name.startswith("."):
                continue
            name = QStandardItem(self._icons.icon(QFileIconProvider.IconType.Folder if e.is_dir
                                                  else QFileIconProvider.IconType.File), e.name + ("@" if e.is_link and not e.is_dir else ""))
            name.setData(e, ENTRY_ROLE)
            size = QStandardItem("" if e.is_dir else human(e.size)); size.setData(-1 if e.is_dir else e.size, SIZE_ROLE)
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            mt = QStandardItem(datetime.fromtimestamp(e.mtime).strftime("%Y-%m-%d %H:%M") if e.mtime else "")
            mt.setData(e.mtime, SIZE_ROLE)
            perm = QStandardItem(e.perm_string)
            for it in (name, size, mt, perm):
                it.setEditable(False)
            self.model.appendRow([name, size, mt, perm])
            shown += 1
        self.statusBar().showMessage(f"{shown} items" + ("" if self.show_hidden else " (hidden files not shown)"))
        self.a_back.setEnabled(bool(self.back)); self.a_fwd.setEnabled(bool(self.fwd))

    def go(self, path): self.spawn(self._navigate(path))
    def refresh(self): self.spawn(self._navigate(self.cwd, push=False))
    def go_up(self): self.go(posixpath.dirname(self.cwd.rstrip("/")) or "/")
    def goto_typed(self): self.go(self.path_edit.text().strip() or "/")

    def go_back(self):
        if self.back:
            self.fwd.append(self.cwd); self.spawn(self._navigate(self.back.pop(), push=False))

    def go_fwd(self):
        if self.fwd:
            self.back.append(self.cwd); self.spawn(self._navigate(self.fwd.pop(), push=False))

    def toggle_hidden(self):
        self.show_hidden = self.a_hidden.isChecked(); self._render()

    # ================= operations =================
    def open_selected(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]
        if e.is_dir:
            self.go(e.path)
            return
        if e.size > OPEN_LIMIT and QMessageBox.question(
                self, "Large file", f"{e.name} is {human(e.size)}. Download it to open?") != QMessageBox.StandardButton.Yes:
            return
        tmp = tempfile.mkdtemp(prefix="sshbrowser-")
        self._queue_download([e], tmp, on_done=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.join(tmp, e.name))))

    def new_folder(self):
        name, ok = QInputDialog.getText(self, "New folder", "Name:", text="untitled folder")
        if ok and name.strip():
            self.spawn(self._then_refresh(self.session.mkdir(posixpath.join(self.cwd, name.strip()))))

    def rename_selected(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        if ok and name.strip() and name != e.name:
            if "/" in name:
                self._error(RemoteError("Names cannot contain “/”."))
                return
            self.spawn(self._then_refresh(self.session.rename(e.path, posixpath.join(self.cwd, name.strip()))))

    def delete_selected(self):
        sel = self.selected()
        if not sel:
            return
        what = sel[0].name if len(sel) == 1 else f"{len(sel)} items"
        box = QMessageBox(QMessageBox.Icon.Warning, "Delete", f"Permanently delete {what} from the remote machine?\nThis cannot be undone.",
                          QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes, self)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        async def run():
            for e in sel:
                await self.session.delete(e)
        self.spawn(self._then_refresh(run()))

    def set_clip(self, mode):
        sel = self.selected()
        if sel:
            self.clip = (mode, [e.path for e in sel])
            self.statusBar().showMessage(f"{len(sel)} item(s) {'copied' if mode == 'copy' else 'cut'}", 4000)

    def paste(self):
        mode, paths = self.clip
        if not paths:
            return
        dest = self.cwd

        async def run():
            for p in paths:
                if mode == "copy":
                    await self.session.copy(p, dest)
                else:
                    await self.session.move(p, dest)
            if mode == "cut":
                self.clip = ("copy", [])
        self.spawn(self._then_refresh(run()))

    def calc_size(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        e = sel[0]
        self.statusBar().showMessage(f"Calculating size of {e.name}…")
        self.spawn(self.session.disk_usage(e.path), lambda n: self.statusBar().showMessage(f"{e.name}: {human(n)}", 10000))

    async def _then_refresh(self, coro):
        await coro
        await self._navigate(self.cwd, push=False)

    # ================= transfers =================
    def upload_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files")
        if files:
            self.queue_upload(files, self.cwd)

    def _dropped(self, paths, idx):
        if not self.session or not paths:
            return
        dest = self.cwd
        if idx.isValid():
            e = self.model.item(self.proxy.mapToSource(idx).row(), 0).data(ENTRY_ROLE)
            if e.is_dir:
                dest = e.path
        self.queue_upload(paths, dest)

    def queue_upload(self, paths, dest):
        for p in paths:
            name = os.path.basename(p.rstrip("/"))
            total = _local_size(p)
            t = Transfer("Upload", f"{name} → {dest}", total=total)

            async def work(progress, p=p, dest=dest, name=name):
                if await self.session.exists(posixpath.join(dest, name)):
                    raise RemoteError(f"{name} already exists in {dest} (not overwritten)")
                await self.session.upload(p, dest, progress)
            self.transfers.add(t, work)

    def download_selected(self):
        sel = self.selected()
        if not sel:
            return
        d = QFileDialog.getExistingDirectory(self, "Download to", str(Path.home() / "Downloads"))
        if d:
            self._queue_download(sel, d)

    def _queue_download(self, entries, local_dir, on_done=None):
        for e in entries:
            t = Transfer("Download", f"{e.name} → {local_dir}", total=0 if e.is_dir else e.size)

            async def work(progress, e=e):
                if os.path.exists(os.path.join(local_dir, e.name)) and on_done is None:
                    raise RemoteError(f"{e.name} already exists in {local_dir} (not overwritten)")
                await self.session.download(e.path, local_dir, progress)
            self.transfers.add(t, work)
            if on_done:
                t._on_done = on_done  # type: ignore[attr-defined]

    def _transfer_finished(self, t: Transfer):
        cb = getattr(t, "_on_done", None)
        if cb and t.status == "Done":
            cb()
        if t.kind == "Upload" and t.status == "Done":
            self.refresh()

    def _transfer_changed(self, t: Transfer):
        row = self._rows.get(t.id)
        if row is None:
            row = self.tt.rowCount(); self.tt.insertRow(row); self._rows[t.id] = row
            self.tt.setItem(row, 0, QTableWidgetItem(t.kind)); self.tt.setItem(row, 1, QTableWidgetItem(t.label))
            self.tt.setItem(row, 3, QTableWidgetItem(""))
            self.tt.setCellWidget(row, 2, QProgressBar())
            self.tt.item(row, 0).setData(Qt.ItemDataRole.UserRole, t.id)
        bar: QProgressBar = self.tt.cellWidget(row, 2)
        if t.total:
            bar.setRange(0, 1000); bar.setValue(min(1000, int(t.done * 1000 / t.total)))
        else:
            bar.setRange(0, 0 if t.status == "Running" else 1); bar.setValue(1 if t.status == "Done" else 0)
        txt = t.status + (f" — {t.error}" if t.error else (f" — {t.current}" if t.status == "Running" and t.current else ""))
        self.tt.item(row, 3).setText(txt); self.tt.item(row, 3).setToolTip(txt)

    def cancel_selected_transfer(self):
        for idx in self.tt.selectionModel().selectedRows():
            tid = self.tt.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole)
            for t in self.transfers.items:
                if t.id == tid:
                    self.transfers.cancel(t)

    def clear_transfers(self):
        self.transfers.clear_finished()
        self.tt.setRowCount(0); self._rows.clear()
        for t in self.transfers.items:
            self._transfer_changed(t)

    # ================= context menu / close =================
    def _context_menu(self, pos):
        m = QMenu(self)
        sel = self.selected()
        if sel:
            if len(sel) == 1:
                m.addAction(self.a_open)
            m.addAction(self.a_download)
            m.addSeparator()
            m.addAction(self.a_copy); m.addAction(self.a_cut)
            if len(sel) == 1:
                m.addAction(self.a_rename); m.addAction(self.a_size)
            m.addAction(self.a_delete)
            m.addSeparator()
        m.addAction(self.a_paste); m.addAction(self.a_mkdir); m.addAction(self.a_upload); m.addAction(self.a_refresh)
        m.exec(self.view.viewport().mapToGlobal(pos))

    def closeEvent(self, ev):
        running = [t for t in self.transfers.items if t.status in ("Queued", "Running")]
        if running and QMessageBox.question(self, "Quit", f"{len(running)} transfer(s) still running. Quit anyway?") \
                != QMessageBox.StandardButton.Yes:
            ev.ignore()
            return
        if self.session:
            self.session.conn.close()
        ev.accept()


def _local_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total
