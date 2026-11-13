"""Host profiles: stored as JSON; passwords (optional) live in the macOS Keychain."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError
except Exception:  # pragma: no cover - keyring missing/unusable
    keyring = None
    KeyringError = PasswordDeleteError = Exception

KEYCHAIN_SERVICE = "SSH Browser"


def config_dir() -> Path:
    override = os.environ.get("SSHBROWSER_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "SSHBrowser"


@dataclass
class HostProfile:
    """One remote machine.

    `host` may be an IP, a DNS name, or an alias from ~/.ssh/config.
    Leave `key_file` and the password empty for passwordless sessions: the
    ssh-agent and the default keys in ~/.ssh are tried automatically.
    """

    name: str
    host: str
    username: str = ""
    port: int = 22
    key_file: str = ""
    start_path: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    # -- password (Keychain, never written to the JSON file) --
    def get_password(self) -> str | None:
        if keyring is None:
            return None
        try:
            return keyring.get_password(KEYCHAIN_SERVICE, self.id)
        except KeyringError:
            return None

    def set_password(self, password: str | None) -> None:
        if keyring is None:
            return
        try:
            if password:
                keyring.set_password(KEYCHAIN_SERVICE, self.id, password)
            else:
                self.clear_password()
        except KeyringError:
            pass

    def clear_password(self) -> None:
        if keyring is None:
            return
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, self.id)
        except (PasswordDeleteError, KeyringError):
            pass


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "hosts.json")
        self.profiles: list[HostProfile] = []
        self.load()

    def load(self) -> None:
        self.profiles = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        known = set(HostProfile.__dataclass_fields__)
        for item in data.get("hosts", []):
            self.profiles.append(HostProfile(**{k: v for k, v in item.items() if k in known}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hosts": [asdict(p) for p in self.profiles]}, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def upsert(self, profile: HostProfile) -> None:
        for i, p in enumerate(self.profiles):
            if p.id == profile.id:
                self.profiles[i] = profile
                break
        else:
            self.profiles.append(profile)
        self.save()

    def remove(self, profile: HostProfile) -> None:
        profile.clear_password()
        self.profiles = [p for p in self.profiles if p.id != profile.id]
        self.save()
