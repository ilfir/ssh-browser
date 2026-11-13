"""Async SFTP layer. Everything goes through the remote's stock sftp-server, so
nothing is installed or left running on the remote machine. The only exec calls
(`cp -R`, `rm -rf`) are short-lived single processes, used where SFTP would need
one round trip per file or would have to move data through this machine."""
from __future__ import annotations

import asyncio
import posixpath
import shlex
import stat as statmod
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import asyncssh

from .config import HostProfile

ProgressCb = Callable[[str, int, int], None]  # (current file, bytes done, bytes total)


class RemoteError(Exception):
    pass


class HostKeyUnknown(Exception):
    def __init__(self, host: str, port: int, fingerprint: str, key):
        super().__init__(f"Unknown host key for {host}:{port}")
        self.host, self.port, self.fingerprint, self.key = host, port, fingerprint, key


@dataclass
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    is_link: bool
    size: int
    mtime: int
    perms: int

    @property
    def perm_string(self) -> str:
        return statmod.filemode(self.perms) if self.perms else ""


def _quote(path: str) -> str:
    return shlex.quote(path)


def _check_safe(path: str) -> None:
    p = posixpath.normpath(path) if path else ""
    if not p or p in ("/", ".", "..", "~") or not p.startswith("/"):
        raise RemoteError(f"Refusing to operate on {path!r}")
    if p in ("/Users", "/System", "/Library", "/Applications", "/private", "/usr", "/bin", "/etc", "/var"):
        raise RemoteError(f"Refusing to operate on system folder {path!r}")


def resolve_endpoint(profile: HostProfile) -> tuple[str, int]:
    """Resolve ~/.ssh/config aliases to the real host/port (local lookup only)."""
    host, port = profile.host, profile.port
    try:
        out = subprocess.run(
            ["ssh", "-G", "-p", str(port), *(["-l", profile.username] if profile.username else []), host],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            k, _, v = line.partition(" ")
            if k == "hostname" and v:
                host = v
            elif k == "port" and v.isdigit() and profile.port == 22:
                port = int(v)
    except Exception:
        pass
    return host, port


def known_hosts_path() -> Path:
    return Path.home() / ".ssh" / "known_hosts"


async def fetch_host_key(profile: HostProfile):
    host, port = resolve_endpoint(profile)
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port), 15)
    return host, port, key


def trust_host_key(host: str, port: int, key, path: Path | None = None) -> None:
    path = path or known_hosts_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    pattern = host if port == 22 else f"[{host}]:{port}"
    line = f"{pattern} {key.export_public_key('openssh').decode().strip()}\n"
    with open(path, "a") as f:
        f.write(line)


class RemoteSession:
    def __init__(self, conn: asyncssh.SSHClientConnection, sftp: asyncssh.SFTPClient, profile: HostProfile):
        self.conn, self.sftp, self.profile = conn, sftp, profile

    # ---------------- connection ----------------
    @classmethod
    async def connect(cls, profile: HostProfile, password: str | None = None,
                      known_hosts=()) -> "RemoteSession":
        """Connect using, in order: explicit key file, ssh-agent / ~/.ssh keys, password.
        Leave password empty for passwordless sessions. `known_hosts=()` means the
        default (~/.ssh/known_hosts); unknown hosts raise HostKeyUnknown."""
        kwargs: dict = dict(
            port=profile.port,
            keepalive_interval=30,
            connect_timeout=15,
        )
        if profile.username:
            kwargs["username"] = profile.username
        if profile.key_file:
            kwargs["client_keys"] = [str(Path(profile.key_file).expanduser())]
        if password:
            kwargs["password"] = password
        if known_hosts != ():
            kwargs["known_hosts"] = known_hosts
        try:
            conn = await asyncssh.connect(profile.host, **kwargs)
        except asyncssh.HostKeyNotVerifiable:
            host, port, key = await fetch_host_key(profile)
            raise HostKeyUnknown(host, port, key.get_fingerprint(), key) from None
        sftp = await conn.start_sftp_client()
        return cls(conn, sftp, profile)

    async def close(self) -> None:
        self.sftp.exit()
        self.conn.close()
        try:
            await asyncio.wait_for(self.conn.wait_closed(), 5)
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        return self.conn is not None and not self.conn.is_closed()

    # ---------------- reading ----------------
    async def home(self) -> str:
        return await self.sftp.realpath(self.profile.start_path or ".")

    async def realpath(self, path: str) -> str:
        return await self.sftp.realpath(path)

    async def listdir(self, path: str) -> list[RemoteEntry]:
        try:
            names = await self.sftp.readdir(path)
        except asyncssh.SFTPError as e:
            raise RemoteError(f"{path}: {e.reason or e}") from e
        entries: list[RemoteEntry] = []
        for n in names:
            name = n.filename if isinstance(n.filename, str) else n.filename.decode(errors="replace")
            if name in (".", ".."):
                continue
            a = n.attrs
            full = posixpath.join(path, name)
            is_link = a.type == asyncssh.FILEXFER_TYPE_SYMLINK
            is_dir = a.type == asyncssh.FILEXFER_TYPE_DIRECTORY
            size, mtime, perms = a.size or 0, a.mtime or 0, a.permissions or 0
            if is_link:  # only symlinks cost an extra stat
                try:
                    t = await self.sftp.stat(full)
                    is_dir = t.type == asyncssh.FILEXFER_TYPE_DIRECTORY
                    size = t.size or 0
                except asyncssh.SFTPError:
                    pass
            entries.append(RemoteEntry(name, full, is_dir, is_link, size, mtime, perms))
        entries.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
        return entries

    async def exists(self, path: str) -> bool:
        return await self.sftp.exists(path)

    async def read_head(self, path: str, limit: int = 256 * 1024) -> bytes:
        async with self.sftp.open(path, "rb") as f:
            return await f.read(limit)

    # ---------------- modifying ----------------
    async def mkdir(self, path: str) -> None:
        try:
            await self.sftp.mkdir(path)
        except asyncssh.SFTPError as e:
            raise RemoteError(f"{path}: {e.reason or e}") from e

    async def rename(self, old: str, new: str) -> None:
        try:
            if await self.sftp.exists(new):
                raise RemoteError(f"{posixpath.basename(new)} already exists")
            await self.sftp.rename(old, new)
        except asyncssh.SFTPError as e:
            raise RemoteError(f"{old}: {e.reason or e}") from e

    async def delete(self, entry: RemoteEntry) -> None:
        _check_safe(entry.path)
        try:
            if entry.is_dir and not entry.is_link:
                # one short-lived process instead of one round trip per file
                await self._exec(f"rm -rf -- {_quote(entry.path)}")
            else:
                await self.sftp.remove(entry.path)
        except asyncssh.SFTPError as e:
            raise RemoteError(f"{entry.path}: {e.reason or e}") from e

    async def copy(self, src: str, dst_dir: str) -> str:
        """Server-side copy (no data passes through this machine). Returns new path."""
        target = await self._unique(dst_dir, posixpath.basename(src))
        await self._exec(f"cp -R -- {_quote(src)} {_quote(target)}")
        return target

    async def move(self, src: str, dst_dir: str) -> str:
        target = posixpath.join(dst_dir, posixpath.basename(src))
        if await self.sftp.exists(target):
            raise RemoteError(f"{posixpath.basename(src)} already exists in {dst_dir}")
        await self.rename(src, target)
        return target

    async def chmod(self, path: str, mode: int) -> None:
        await self.sftp.chmod(path, mode)

    async def _unique(self, directory: str, name: str) -> str:
        base, ext = posixpath.splitext(name)
        cand, i = posixpath.join(directory, name), 1
        while await self.sftp.exists(cand):
            suffix = " copy" if i == 1 else f" copy {i}"
            cand = posixpath.join(directory, f"{base}{suffix}{ext}")
            i += 1
        return cand

    async def _exec(self, cmd: str) -> str:
        res = await self.conn.run(cmd, check=False)
        if res.exit_status != 0:
            raise RemoteError((res.stderr or f"command failed: {cmd}").strip())
        return res.stdout or ""

    async def disk_usage(self, path: str) -> int:
        """On-demand only (walks the tree on the remote)."""
        out = await self._exec(f"nice du -sk -- {_quote(path)}")
        return int(out.split()[0]) * 1024

    # ---------------- transfers ----------------
    async def upload(self, local: str, remote_dir: str, progress: ProgressCb | None = None) -> None:
        def handler(src, dst, done, total):
            if progress:
                progress(Path(src.decode() if isinstance(src, bytes) else src).name, done, total)
        try:
            await self.sftp.put(local, remote_dir.rstrip("/") + "/", recurse=True,
                                preserve=True, progress_handler=handler)
        except asyncssh.SFTPError as e:
            raise RemoteError(f"{local}: {e.reason or e}") from e

    async def download(self, remote: str, local_dir: str, progress: ProgressCb | None = None) -> None:
        def handler(src, dst, done, total):
            if progress:
                progress(posixpath.basename((src.decode() if isinstance(src, bytes) else src)), done, total)
        try:
            await self.sftp.get(remote, local_dir, recurse=True, preserve=True, progress_handler=handler)
        except asyncssh.SFTPError as e:
            raise RemoteError(f"{remote}: {e.reason or e}") from e
