"""The network guard must hold on every platform, including Windows' loopback self-pipe."""

from __future__ import annotations

import asyncio
import socket

import pytest
from pytest_socket import (
    SocketBlockedError,
    SocketConnectBlockedError,
    enable_socket,
    socket_allow_hosts,
)

from helpers import LOOPBACK_HOSTS, socket_policy


def test_windows_policy_allows_only_loopback_and_posix_disables_sockets() -> None:
    assert socket_policy("win32") == {"disable_socket": False, "allow_hosts": LOOPBACK_HOSTS}
    assert socket_policy("linux") == {"disable_socket": True, "allow_hosts": None}
    assert socket_policy("darwin") == {"disable_socket": True, "allow_hosts": None}
    assert set(LOOPBACK_HOSTS.split(",")) == {"127.0.0.1", "::1"}


def test_current_platform_still_blocks_external_connections() -> None:
    """Whatever the platform policy is, a documentation address must stay unreachable."""
    with (
        pytest.raises((SocketBlockedError, SocketConnectBlockedError)),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
    ):
        sock.settimeout(0.2)
        sock.connect(("192.0.2.1", 80))  # TEST-NET-1, never routable


def test_loopback_allowlist_blocks_external_hosts_but_allows_the_event_loop() -> None:
    """The Windows policy, exercised on any platform: loopback only."""
    enable_socket()  # lift the POSIX --disable-socket guard; the plugin restores it afterwards
    socket_allow_hosts(LOOPBACK_HOSTS.split(","), allow_unix_socket=True)
    loop = asyncio.new_event_loop()  # creates the self-pipe socketpair on Windows
    loop.close()
    with (
        pytest.raises(SocketConnectBlockedError),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
    ):
        sock.settimeout(0.2)
        sock.connect(("192.0.2.1", 80))
