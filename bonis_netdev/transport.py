"""How a driver reads a device. The prototype transports are READ-ONLY: anything the
classifier does not call a read is refused before it reaches the wire."""
from __future__ import annotations

import subprocess

from .classify import Effect, effect_of

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=8", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa,ssh-dss", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa,ssh-dss",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
]


class WriteRefused(RuntimeError):
    pass


class Transport:
    host: str

    def read(self, command: str) -> str:
        raise NotImplementedError


class SshReadTransport(Transport):
    """Password SSH via sshpass; credential sets are tried in order (the fleet has several)."""

    def __init__(self, host: str, credentials: list[tuple[str, str]]):
        self.host = host
        self._credentials = credentials
        self._cred: tuple[str, str] | None = None

    def _ssh(self, cred, command):
        user, password = cred
        return subprocess.run(["sshpass", "-p", password, "ssh", *SSH_OPTS, f"{user}@{self.host}", command],
                              capture_output=True, text=True, timeout=30)

    def read(self, command: str) -> str:
        if effect_of(command) != Effect.READ:
            raise WriteRefused(f"read-only transport refuses {command!r}")
        for cred in ([self._cred] if self._cred else self._credentials):
            p = self._ssh(cred, command)
            if p.returncode == 0 or p.stdout.strip():
                self._cred = cred
                return p.stdout
        raise RuntimeError(f"{self.host}: no credential worked for {command!r}")


class FakeTransport(Transport):
    """Canned answers, for tests."""

    def __init__(self, host: str, answers: dict[str, str]):
        self.host = host
        self.answers = answers
        self.asked: list[str] = []

    def read(self, command: str) -> str:
        if effect_of(command) != Effect.READ:
            raise WriteRefused(command)
        self.asked.append(command)
        if command not in self.answers:
            raise KeyError(f"no canned answer for {command!r}")
        return self.answers[command]
