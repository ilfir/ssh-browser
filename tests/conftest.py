import asyncio
import subprocess

import asyncssh
import pytest
import pytest_asyncio

from sshbrowser.config import HostProfile


class _Server(asyncssh.SSHServer):
    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "tester" and password == "secret"


async def _handle_process(process):
    cmd = process.command or ""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    process.stdout.write(r.stdout)
    process.stderr.write(r.stderr)
    process.exit(r.returncode)


@pytest_asyncio.fixture
async def server():
    key = asyncssh.generate_private_key("ssh-ed25519")
    acceptor = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=_Server, server_host_keys=[key],
        process_factory=_handle_process, sftp_factory=True, allow_scp=False,
    )
    port = acceptor.sockets[0].getsockname()[1]
    yield port
    acceptor.close()
    await acceptor.wait_closed()


@pytest.fixture
def profile(server):
    return HostProfile(name="t", host="127.0.0.1", username="tester", port=server)
