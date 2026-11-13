"""Drives the real MainWindow (offscreen) against an in-process SFTP server."""
import asyncio
import os
import subprocess

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import asyncssh
import qasync
from PySide6.QtWidgets import QApplication

from sshbrowser.config import HostProfile, ProfileStore
from sshbrowser.main_window import ENTRY_ROLE, MainWindow
from sshbrowser.sftp_service import RemoteSession


class _Srv(asyncssh.SSHServer):
    def password_auth_supported(self): return True
    def validate_password(self, u, p): return (u, p) == ("tester", "secret")


async def _proc(process):
    r = subprocess.run(process.command or "", shell=True, capture_output=True, text=True)
    process.stdout.write(r.stdout); process.stderr.write(r.stderr); process.exit(r.returncode)


def test_window_end_to_end(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    from PySide6.QtWidgets import QMessageBox
    def boom(*a, **k):
        raise AssertionError(f"modal dialog shown: {a[1:3]}")
    monkeypatch.setattr(QMessageBox, "warning", boom)
    monkeypatch.setattr(QMessageBox, "question", boom)

    orig = RemoteSession.connect.__func__
    async def connect(cls, profile, password=None, known_hosts=()):
        return await orig(cls, profile, password, known_hosts=None)
    monkeypatch.setattr(RemoteSession, "connect", classmethod(connect))

    remote = tmp_path / "remote"; remote.mkdir()
    (remote / "a.txt").write_text("a"); (remote / ".hidden").write_text("h"); (remote / "sub").mkdir()
    local = tmp_path / "up.bin"; local.write_bytes(b"z" * 4096)
    dl = tmp_path / "dl"; dl.mkdir()

    async def scenario():
        key = asyncssh.generate_private_key("ssh-ed25519")
        srv = await asyncssh.listen("127.0.0.1", 0, server_factory=_Srv, server_host_keys=[key],
                                    process_factory=_proc, sftp_factory=True)
        port = srv.sockets[0].getsockname()[1]
        store = ProfileStore(tmp_path / "hosts.json")
        prof = HostProfile(name="t", host="127.0.0.1", username="tester", port=port, start_path=str(remote))
        store.upsert(prof)
        assert ProfileStore(tmp_path / "hosts.json").profiles[0].host == "127.0.0.1"
        assert "secret" not in (tmp_path / "hosts.json").read_text()

        w = MainWindow(store)
        await w._connect(prof, "secret")
        names = lambda: [w.model.item(r, 0).data(ENTRY_ROLE).name for r in range(w.model.rowCount())]
        assert sorted(names()) == ["a.txt", "sub"]              # hidden filtered
        w.a_hidden.setChecked(True); w.toggle_hidden()
        assert ".hidden" in names()

        w.queue_upload([str(local)], str(remote))
        while any(t.status in ("Queued", "Running") for t in w.transfers.items):
            await asyncio.sleep(0.05)
        assert w.transfers.items[0].status == "Done", w.transfers.items[0].error
        assert (remote / "up.bin").stat().st_size == 4096

        w.queue_upload([str(local)], str(remote))               # existing -> refused, not overwritten
        while any(t.status in ("Queued", "Running") for t in w.transfers.items):
            await asyncio.sleep(0.05)
        assert w.transfers.items[1].status == "Failed"

        entry = next(e for e in w.entries if e.name == "a.txt")
        w._queue_download([entry], str(dl))
        while any(t.status in ("Queued", "Running") for t in w.transfers.items):
            await asyncio.sleep(0.05)
        assert (dl / "a.txt").read_text() == "a"

        await w._navigate(str(remote / "sub"))
        assert w.cwd.endswith("sub") and w.back
        w.go_up(); await asyncio.sleep(0.5)
        assert w.cwd == str((remote).resolve())
        await w.session.close(); srv.close()

    with loop:
        loop.run_until_complete(asyncio.wait_for(scenario(), 30))
