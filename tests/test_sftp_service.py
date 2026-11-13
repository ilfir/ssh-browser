import asyncssh
import pytest

from sshbrowser.sftp_service import HostKeyUnknown, RemoteError, RemoteSession

pytestmark = pytest.mark.asyncio


async def _connect(profile):
    return await RemoteSession.connect(profile, password="secret", known_hosts=None)


async def test_unknown_host_key_is_reported(profile, tmp_path, monkeypatch):
    (tmp_path / "empty").write_text("")
    with pytest.raises(HostKeyUnknown) as ei:
        await RemoteSession.connect(profile, password="secret", known_hosts=str(tmp_path / "empty"))
    assert ei.value.fingerprint.startswith("SHA256:")


async def test_wrong_password_rejected(profile):
    with pytest.raises(asyncssh.PermissionDenied):
        await RemoteSession.connect(profile, password="nope", known_hosts=None)


async def test_list_rename_mkdir_delete(profile, tmp_path):
    (tmp_path / "b.txt").write_text("hi")
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "inner.txt").write_text("x")
    s = await _connect(profile)
    try:
        entries = await s.listdir(str(tmp_path))
        assert [e.name for e in entries] == ["A", "b.txt"]  # dirs first
        assert entries[0].is_dir and not entries[1].is_dir and entries[1].size == 2

        await s.mkdir(str(tmp_path / "new"))
        assert (tmp_path / "new").is_dir()

        await s.rename(str(tmp_path / "b.txt"), str(tmp_path / "c.txt"))
        assert (tmp_path / "c.txt").exists()
        with pytest.raises(RemoteError):
            await s.rename(str(tmp_path / "c.txt"), str(tmp_path / "new"))

        entries = {e.name: e for e in await s.listdir(str(tmp_path))}
        await s.delete(entries["A"])
        await s.delete(entries["c.txt"])
        assert not (tmp_path / "A").exists() and not (tmp_path / "c.txt").exists()
    finally:
        await s.close()


async def test_delete_refuses_dangerous_paths(profile):
    from sshbrowser.sftp_service import RemoteEntry
    s = await _connect(profile)
    try:
        for p in ("/", "", "relative", "/Users"):
            with pytest.raises(RemoteError):
                await s.delete(RemoteEntry("x", p, True, False, 0, 0, 0))
    finally:
        await s.close()


async def test_upload_download_roundtrip(profile, tmp_path):
    local = tmp_path / "local"; local.mkdir()
    (local / "dir").mkdir(); (local / "dir" / "f.bin").write_bytes(b"\x00" * 5000)
    (local / "one.txt").write_text("hello")
    remote = tmp_path / "remote"; remote.mkdir()
    out = tmp_path / "out"; out.mkdir()
    seen = []
    s = await _connect(profile)
    try:
        await s.upload(str(local / "dir"), str(remote), lambda n, d, t: seen.append((n, d, t)))
        await s.upload(str(local / "one.txt"), str(remote))
        assert (remote / "dir" / "f.bin").stat().st_size == 5000
        assert (remote / "one.txt").read_text() == "hello"
        assert seen and seen[-1][1] == seen[-1][2] == 5000

        await s.download(str(remote / "dir"), str(out))
        await s.download(str(remote / "one.txt"), str(out))
        assert (out / "dir" / "f.bin").stat().st_size == 5000
        assert (out / "one.txt").read_text() == "hello"
    finally:
        await s.close()


async def test_copy_and_move(profile, tmp_path):
    (tmp_path / "d").mkdir(); (tmp_path / "d" / "f.txt").write_text("1")
    (tmp_path / "x.txt").write_text("x")
    (tmp_path / "dest").mkdir()
    s = await _connect(profile)
    try:
        new = await s.copy(str(tmp_path / "x.txt"), str(tmp_path))
        assert new.endswith("x copy.txt") and (tmp_path / "x copy.txt").exists()
        new2 = await s.copy(str(tmp_path / "x.txt"), str(tmp_path))
        assert new2.endswith("x copy 2.txt")
        await s.copy(str(tmp_path / "d"), str(tmp_path / "dest"))
        assert (tmp_path / "dest" / "d" / "f.txt").exists()
        await s.move(str(tmp_path / "x.txt"), str(tmp_path / "dest"))
        assert (tmp_path / "dest" / "x.txt").exists() and not (tmp_path / "x.txt").exists()
    finally:
        await s.close()


async def test_symlink_to_dir_is_dir(profile, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    s = await _connect(profile)
    try:
        e = {x.name: x for x in await s.listdir(str(tmp_path))}
        assert e["link"].is_link and e["link"].is_dir
    finally:
        await s.close()
