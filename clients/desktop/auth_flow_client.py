"""Small synchronous WebSocket client for pre-login account flows.

The main client connection is owned by :class:`NetworkManager` and only exists
after the login dialog closes. Registration, email verification, and password
recovery happen before that point, so they use this short-lived connection.
"""

from __future__ import annotations

import json
import ssl

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from network_manager import _prefer_ipv4_loopback_url


class AuthFlowError(Exception):
    """Raised when an account flow fails or the server rejects the request."""


class AuthFlowSession:
    """A short-lived connection for unauthenticated account operations.

    Usage::

        with AuthFlowSession(server_url) as session:
            session.send({"type": "register", ...})
            pending = session.receive({"registration_pending", "registration_error"})
    """

    def __init__(self, server_url: str, *, ssl_context: ssl.SSLContext | None = None):
        """Open a connection to the server."""
        if not server_url:
            raise AuthFlowError("No server URL is configured.")

        kwargs = {}
        if server_url.startswith("wss://"):
            kwargs["ssl"] = ssl_context or ssl.create_default_context()
            connect_url = server_url
        else:
            connect_url = _prefer_ipv4_loopback_url(server_url)

        try:
            self._ws = connect(
                connect_url,
                open_timeout=15,
                close_timeout=3,
                **kwargs,
            )
        except (OSError, TimeoutError, ssl.SSLError, ConnectionClosed) as exc:
            raise AuthFlowError(f"Could not connect to the server: {exc}") from exc

    def __enter__(self) -> "AuthFlowSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection quietly."""
        try:
            self._ws.close()
        except Exception:
            pass

    def send(self, packet: dict) -> None:
        """Send one JSON packet."""
        try:
            self._ws.send(json.dumps(packet))
        except (OSError, ConnectionClosed) as exc:
            raise AuthFlowError(f"Could not send the request: {exc}") from exc

    def receive(
        self, expected_types: set[str], *, timeout: float = 20.0
    ) -> dict:
        """Wait for one packet of an expected type.

        Speak and unrelated packets are ignored. Disconnect packets become
        :class:`AuthFlowError`.
        """
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuthFlowError("The server did not respond in time.")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise AuthFlowError("The server did not respond in time.") from exc
            except (OSError, ConnectionClosed) as exc:
                raise AuthFlowError("The server closed the connection.") from exc

            try:
                packet = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise AuthFlowError("The server sent an invalid response.") from exc
            packet_type = packet.get("type")
            if packet_type in expected_types:
                return packet
            if packet_type == "disconnect":
                raise AuthFlowError(packet.get("message") or "The request was rejected.")
