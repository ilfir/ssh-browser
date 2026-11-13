from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout)

from .config import HostProfile, ProfileStore


class HostEditDialog(QDialog):
    def __init__(self, profile: HostProfile | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit host" if profile else "New host")
        self.profile = profile or HostProfile(name="", host="")
        self._had_password = bool(profile and profile.get_password())
        p = self.profile

        self.name = QLineEdit(p.name)
        self.host = QLineEdit(p.host); self.host.setPlaceholderText("IP, hostname, or ~/.ssh/config alias")
        self.user = QLineEdit(p.username); self.user.setPlaceholderText("blank = ssh config / current user")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(p.port)
        self.key = QLineEdit(p.key_file); self.key.setPlaceholderText("optional; blank = ssh-agent and ~/.ssh keys")
        browse = QPushButton("Choose…"); browse.clicked.connect(self._pick_key)
        keyrow = QHBoxLayout(); keyrow.addWidget(self.key); keyrow.addWidget(browse)
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("saved" if self._had_password else "optional; blank = passwordless")
        self.start = QLineEdit(p.start_path); self.start.setPlaceholderText("blank = home folder")

        form = QFormLayout()
        form.addRow("Name", self.name); form.addRow("Host", self.host); form.addRow("Username", self.user)
        form.addRow("Port", self.port); form.addRow("Key file", keyrow); form.addRow("Password", self.password)
        form.addRow("Start folder", self.start)
        note = QLabel("Passwords are stored in the macOS Keychain, never in the config file.\n"
                      "Leave key file and password empty for passwordless (agent / default key) login.")
        note.setWordWrap(True); note.setStyleSheet("color: gray; font-size: 11px;")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept); buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self); lay.addLayout(form); lay.addWidget(note); lay.addWidget(buttons)
        self.resize(460, 0)

    def _pick_key(self):
        f, _ = QFileDialog.getOpenFileName(self, "Private key", str(__import__("pathlib").Path.home() / ".ssh"))
        if f:
            self.key.setText(f)

    def _accept(self):
        if not self.host.text().strip():
            QMessageBox.warning(self, "Host required", "Enter an IP, hostname or ssh config alias.")
            return
        p = self.profile
        p.host = self.host.text().strip()
        p.name = self.name.text().strip() or p.host
        p.username = self.user.text().strip()
        p.port = self.port.value()
        p.key_file = self.key.text().strip()
        p.start_path = self.start.text().strip()
        if self.password.text():
            p.set_password(self.password.text())
        self.accept()


class HostsDialog(QDialog):
    def __init__(self, store: ProfileStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hosts")
        self.store = store
        self.list = QListWidget(); self.list.itemDoubleClicked.connect(lambda _: self.edit())
        add, edit, rm = QPushButton("Add…"), QPushButton("Edit…"), QPushButton("Remove")
        add.clicked.connect(self.add); edit.clicked.connect(self.edit); rm.clicked.connect(self.remove)
        side = QVBoxLayout(); [side.addWidget(b) for b in (add, edit, rm)]; side.addStretch()
        row = QHBoxLayout(); row.addWidget(self.list); row.addLayout(side)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); close.rejected.connect(self.accept)
        lay = QVBoxLayout(self); lay.addLayout(row); lay.addWidget(close)
        self.resize(480, 300); self.refresh()

    def refresh(self):
        self.list.clear()
        for p in self.store.profiles:
            self.list.addItem(f"{p.name}  —  {p.username + '@' if p.username else ''}{p.host}")

    def _current(self):
        r = self.list.currentRow()
        return self.store.profiles[r] if r >= 0 else None

    def add(self):
        d = HostEditDialog(None, self)
        if d.exec():
            self.store.upsert(d.profile); self.refresh()

    def edit(self):
        p = self._current()
        if p and HostEditDialog(p, self).exec():
            self.store.upsert(p); self.refresh()

    def remove(self):
        p = self._current()
        if p and QMessageBox.question(self, "Remove host", f"Remove “{p.name}” and its saved password?") \
                == QMessageBox.StandardButton.Yes:
            self.store.remove(p); self.refresh()
