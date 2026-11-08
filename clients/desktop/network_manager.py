"""Network manager for WebSocket communication with server."""

import asyncio
import json
import logging
import platform as platform_mod
import threading
import hashlib
import os
import tempfile
import time
from urllib.parse import urlparse, urlunparse

import wx
import websockets
import ssl
from jsonschema import ValidationError as SchemaValidationError
from websockets.asyncio.client import connect

from certificate_prompt import CertificatePromptDialog, CertificateInfo
from packet_validator import validate_incoming, validate_outgoing

LOG = logging.getLogger(__name__)


def _prefer_ipv4_loopback_url(server_url: str) -> str:
    """Use IPv4 for insecure localhost URLs when possible.

    On Windows, ``localhost`` often resolves to ``::1`` first. A server bound
    to ``0.0.0.0`` refuses that connection only after a multi-second timeout,
    so the client appears slow even though the server is local. ``wss://`` is
    left untouched because TLS certificate validation depends on the hostname.
    """
    parsed = urlparse(server_url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "ws" or hostname != "localhost":
        return server_url

    netloc = "127.0.0.1"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


class TLSUserDeclinedError(Exception):
    """Raised when the user declines to trust a presented TLS certificate."""

    pass


class NetworkManager:
    """Manages WebSocket connection to Play Palace server."""

    def __init__(self, main_window):
        """
        Initialize network manager.

        Args:
            main_window: Reference to MainWindow for callbacks
        """
        self.main_window = main_window
        self.ws = None
        self.connected = False
        self.username = None
        self.thread = None
        self.loop = None
        self.should_stop = False
        self.server_url = None
        self.server_id = None
        self.session_token = None
        self.session_expires_at = None
        self.refresh_token = None
        self.refresh_expires_at = None
        self._validation_errors = 0

    def _validate_outgoing_packet(self, packet: dict) -> bool:
        """Validate a packet before sending; logs and blocks invalid payloads."""
        try:
            validate_outgoing(packet)
            return True
        except SchemaValidationError as exc:
            self._validation_errors += 1
            wx.CallAfter(
                self.main_window.add_history,
                f"Blocked invalid outgoing packet #{self._validation_errors}: {exc.message}",
                "activity",
            )
            return False

    def _validate_incoming_packet(self, packet: dict) -> bool:
        """Validate incoming data from the server."""
        try:
            validate_incoming(packet)
            return True
        except SchemaValidationError as exc:
            self._validation_errors += 1
            wx.CallAfter(
                self.main_window.add_history,
                f"Ignored invalid server packet #{self._validation_errors}: {exc.message}",
                "activity",
            )
            return False

    def connect(self, server_url, username, password, refresh_token=None, refresh_expires_at=None):
        """
        Connect to server.

        Args:
            server_url: WebSocket URL (e.g., "ws://localhost:8000")
            username: Username for authorization
            password: Password for authorization
            refresh_token: Refresh token for session renewal
            refresh_expires_at: Refresh token expiration (epoch seconds)
        """
        try:
            # Wait for old thread to finish if it exists
            if self.thread and self.thread.is_alive():
                self.should_stop = True
                # Wait up to 2 seconds for thread to finish
                self.thread.join(timeout=2.0)

            if self.server_url and (self.server_url != server_url or self.username != username):
                self.session_token = None
                self.session_expires_at = None
                self.refresh_token = None
                self.refresh_expires_at = None

            self.username = username
            self.should_stop = False
            self.server_url = server_url
            self.server_id = getattr(self.main_window, "server_id", None)
            # Keep refresh token state aligned with the credentials used for this connection.
            self.refresh_token = refresh_token or None
            self.refresh_expires_at = refresh_expires_at if refresh_token else None

            # Start async thread
            self.thread = threading.Thread(
                target=self._run_async_loop,
                args=(server_url, username, password),
                daemon=True,
            )
            self.thread.start()

            return True
        except Exception:
            import traceback

            traceback.print_exc()
            return False

    def _run_async_loop(self, server_url, username, password):
        """Run the async event loop in a thread."""
        try:
            # Create new event loop for this thread
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            # Run the connection coroutine
            self.loop.run_until_complete(self._connect_and_listen(server_url, username, password))
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            self.loop.close()

    async def _connect_and_listen(self, server_url, username, password):
        """Connect to server and listen for messages."""
        websocket = None
        try:
            websocket = await self._open_connection(server_url)
            self.ws = websocket
            self.connected = True

            if self._refresh_valid():
                await self._send_refresh_session(websocket, username)
                # Wait for the server's response to the refresh attempt.
                # If it fails (or the server silently drops the packet),
                # fall back to password-based authorize.
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                    packet = json.loads(message)
                    if packet.get("type") == "refresh_session_failure":
                        self.session_token = None
                        self.session_expires_at = None
                        self.refresh_token = None
                        self.refresh_expires_at = None
                        await self._send_authorize(websocket, username, password)
                    else:
                        wx.CallAfter(self._handle_packet, packet)
                except asyncio.TimeoutError:
                    self.session_token = None
                    self.session_expires_at = None
                    self.refresh_token = None
                    self.refresh_expires_at = None
                    await self._send_authorize(websocket, username, password)
            else:
                self.session_token = None
                self.session_expires_at = None
                await self._send_authorize(websocket, username, password)

            while not self.should_stop:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    packet = json.loads(message)
                    wx.CallAfter(self._handle_packet, packet)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    break
        except TLSUserDeclinedError:
            wx.CallAfter(
                self.main_window.add_history,
                "TLS certificate was not trusted; connection aborted.",
                "activity",
            )
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            self.connected = False
            self.ws = None
            if websocket:
                try:
                    await websocket.close()
                except (websockets.exceptions.ConnectionClosed, OSError, RuntimeError):
                    import traceback

                    traceback.print_exc()
            if not self.should_stop:
                wx.CallAfter(self.main_window.on_connection_lost)

    async def _send_authorize(self, websocket, username, password):
        """Send the authorize packet after connecting."""
        packet = {
            "type": "authorize",
            "username": username,
            "major": 11,
            "minor": 0,
            "patch": 0,
            "client_type": "Desktop",
            "platform": f"{platform_mod.system()} {platform_mod.release()} {platform_mod.machine()}",
        }
        if self.session_token and self._session_valid():
            packet["session_token"] = self.session_token
        else:
            packet["password"] = password
        if not self._validate_outgoing_packet(packet):
            raise RuntimeError("Client refused to send invalid authorize packet.")
        await websocket.send(json.dumps(packet))

    async def _send_refresh_session(self, websocket, username):
        """Send a refresh token packet after connecting."""
        packet = {
            "type": "refresh_session",
            "refresh_token": self.refresh_token,
            "username": username,
            "client_type": "Desktop",
            "platform": f"{platform_mod.system()} {platform_mod.release()} {platform_mod.machine()}",
        }
        if not self._validate_outgoing_packet(packet):
            raise RuntimeError("Client refused to send invalid refresh packet.")
        await websocket.send(json.dumps(packet))

    def _session_valid(self) -> bool:
        if not self.session_token:
            return False
        if not self.session_expires_at:
            return True
        return time.time() < (self.session_expires_at - 5)

    def _refresh_valid(self) -> bool:
        if not self.refresh_token:
            return False
        if not self.refresh_expires_at:
            return True
        return time.time() < (self.refresh_expires_at - 5)

    async def _open_connection(self, server_url: str):
        """Open a websocket connection, handling TLS verification."""
        if not server_url.startswith("wss://"):
            preferred_url = _prefer_ipv4_loopback_url(server_url)
            if preferred_url != server_url:
                try:
                    return await connect(preferred_url)
                except OSError:
                    # Preserve support for IPv6-only local servers.
                    pass
            return await connect(server_url)

        try:
            websocket = await connect(server_url, ssl=self._build_default_ssl_context())
            await self._verify_pinned_certificate(websocket, server_url)
            return websocket
        except ssl.SSLCertVerificationError:
            websocket = await self._handle_tls_failure(server_url)
            if websocket:
                return websocket
            raise

    def _build_default_ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    async def _handle_tls_failure(self, server_url: str):
        """Recover from TLS verification failure (self-signed certs)."""
        trust_entry = self._get_trusted_certificate_entry()
        if trust_entry:
            return await self._connect_with_trusted_certificate(server_url, trust_entry)

        cert_info = await self._fetch_certificate_info(server_url)
        if not cert_info:
            return None

        if not self._prompt_trust_decision(cert_info):
            raise TLSUserDeclinedError("User declined to trust the certificate.")

        self._store_trusted_certificate(cert_info)
        trust_entry = self._get_trusted_certificate_entry()
        if not trust_entry:
            raise TLSUserDeclinedError("Unable to store trusted certificate.")
        return await self._connect_with_trusted_certificate(server_url, trust_entry)

    async def _connect_with_trusted_certificate(self, server_url: str, trust_entry: dict):
        """Connect using a stored certificate fingerprint (TOFU)."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        websocket = await connect(server_url, ssl=context)
        await self._verify_pinned_certificate(websocket, server_url, trust_entry)
        return websocket

    async def _verify_pinned_certificate(
        self,
        websocket: websockets.WebSocketClientProtocol,
        server_url: str,
        trust_entry: dict | None = None,
    ):
        """Compare presented certificate fingerprint with stored metadata."""
        entry = trust_entry or self._get_trusted_certificate_entry()
        if not entry:
            return

        fingerprint_hex, _, _ = self._extract_peer_certificate(websocket)
        if not fingerprint_hex:
            raise ssl.SSLError("Unable to read peer certificate.")

        expected = entry.get("fingerprint", "").upper()
        if expected != fingerprint_hex.upper():
            await websocket.close()
            raise ssl.SSLError("Trusted certificate fingerprint mismatch.")

    async def _fetch_certificate_info(self, server_url: str) -> CertificateInfo | None:
        """Retrieve certificate information without enforcing trust."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        websocket = None
        try:
            websocket = await connect(server_url, ssl=context)
            fingerprint_hex, cert_dict, pem = self._extract_peer_certificate(websocket)
            if not fingerprint_hex or not pem:
                return None
            host = self._get_server_host(server_url)
            return self._build_certificate_info(cert_dict or {}, fingerprint_hex, pem, host)
        except Exception:
            return None
        finally:
            if websocket:
                try:
                    await websocket.close()
                except (OSError, RuntimeError, websockets.exceptions.ConnectionClosed) as exc:
                    LOG.debug("Failed to close websocket during certificate fetch: %s", exc)

    def _prompt_trust_decision(self, cert_info: CertificateInfo) -> bool:
        """Show the trust dialog on the main thread."""
        decision = {"trust": False}
        done = threading.Event()

        def _show_dialog():
            dlg = CertificatePromptDialog(self.main_window, cert_info)
            result = dlg.ShowModal()
            dlg.Destroy()
            decision["trust"] = result == wx.ID_OK
            done.set()

        wx.CallAfter(_show_dialog)
        done.wait()
        return decision["trust"]

    def _store_trusted_certificate(self, cert_info: CertificateInfo) -> None:
        """Persist the trusted certificate metadata via ConfigManager."""
        manager = getattr(self.main_window, "config_manager", None)
        server_id = getattr(self.main_window, "server_id", None)
        if not manager or not server_id:
            return
        manager.set_trusted_certificate(
            server_id,
            {
                "fingerprint": cert_info.fingerprint_hex,
                "pem": cert_info.pem,
                "host": cert_info.host,
                "common_name": cert_info.common_name,
            },
        )

    def _get_trusted_certificate_entry(self) -> dict | None:
        manager = getattr(self.main_window, "config_manager", None)
        server_id = getattr(self.main_window, "server_id", None)
        if not manager or not server_id:
            return None
        return manager.get_trusted_certificate(server_id)

    def _extract_peer_certificate(self, websocket):
        """Return (hex fingerprint, decoded cert dict, PEM)."""
        if not websocket or not websocket.transport:
            return None, None, None
        ssl_obj = websocket.transport.get_extra_info("ssl_object")
        if not ssl_obj:
            return None, None, None
        der_bytes = ssl_obj.getpeercert(binary_form=True)
        cert_dict = ssl_obj.getpeercert()
        pem = None
        if der_bytes:
            pem = ssl.DER_cert_to_PEM_cert(der_bytes)

        if cert_dict is None and pem:
            cert_dict = self._decode_certificate_dict(pem)

        if not der_bytes or cert_dict is None:
            return None, cert_dict, None
        fingerprint_hex = hashlib.sha256(der_bytes).hexdigest().upper()
        return fingerprint_hex, cert_dict, pem

    def _decode_certificate_dict(self, pem: str) -> dict | None:
        """Decode certificate metadata from PEM when ssl.getpeercert() returns None."""
        tmp_path = None
        try:
            tmp = tempfile.NamedTemporaryFile("w", delete=False)
            tmp.write(pem)
            tmp.flush()
            tmp_path = tmp.name
            tmp.close()
            return ssl._ssl._test_decode_cert(tmp_path)
        except Exception:
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _build_certificate_info(
        self, cert_dict, fingerprint_hex: str, pem: str, host: str
    ) -> CertificateInfo:
        """Convert Python's SSL cert dict into CertificateInfo."""
        cert_dict = self._merge_cert_metadata(cert_dict, pem)
        common_name = self._extract_common_name(cert_dict)
        issuer_text = self._format_issuer(cert_dict)
        sans = [value for kind, value in cert_dict.get("subjectAltName", []) if kind == "DNS"]
        matches = self._certificate_matches_host(common_name, sans, host)
        display_fp = self._format_fingerprint(fingerprint_hex)
        return CertificateInfo(
            host=host,
            common_name=common_name,
            sans=sans,
            issuer=issuer_text,
            valid_from=cert_dict.get("notBefore", ""),
            valid_to=cert_dict.get("notAfter", ""),
            fingerprint=display_fp,
            fingerprint_hex=fingerprint_hex,
            pem=pem,
            matches_host=matches,
        )

    def _merge_cert_metadata(self, cert_dict, pem: str | None) -> dict:
        cert_dict = cert_dict or {}
        if pem and (not cert_dict or "notBefore" not in cert_dict or "notAfter" not in cert_dict):
            decoded = self._decode_certificate_dict(pem)
            if decoded:
                merged = dict(decoded)
                for key, value in cert_dict.items():
                    if value:
                        merged[key] = value
                cert_dict = merged
        return cert_dict

    @staticmethod
    def _extract_common_name(cert_dict: dict) -> str:
        subject = cert_dict.get("subject", [])
        for entry in subject:
            for key, value in entry:
                if key == "commonName":
                    return value
        return ""

    @staticmethod
    def _format_issuer(cert_dict: dict) -> str:
        issuer = []
        for entry in cert_dict.get("issuer", []):
            issuer.append("=".join(entry_part[1] for entry_part in entry))
        return ", ".join(issuer) if issuer else "(unknown)"

    @staticmethod
    def _certificate_matches_host(common_name: str, sans: list[str], host: str) -> bool:
        host_lower = (host or "").lower()
        if not host_lower:
            return False
        if common_name.lower() == host_lower:
            return True
        return any(san.lower() == host_lower for san in sans)

    @staticmethod
    def _format_fingerprint(fingerprint_hex: str) -> str:
        return ":".join(fingerprint_hex[i : i + 2] for i in range(0, len(fingerprint_hex), 2))

    def _get_server_host(self, server_url: str) -> str:
        try:
            return urlparse(server_url).hostname or ""
        except Exception:
            return ""

    def disconnect(self, wait=False, timeout=3.0):
        """
        Disconnect from server.

        Args:
            wait: If True, wait for the thread to fully stop
            timeout: Maximum time to wait for thread to stop (seconds)
        """
        self.should_stop = True
        self.connected = False

        # Close websocket if it exists
        if self.ws and self.loop:
            try:
                # Schedule close in the async loop
                asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
            except (OSError, RuntimeError) as exc:
                LOG.debug("Failed to schedule websocket close: %s", exc)

        # Wait for thread to fully stop if requested
        if wait and self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def send_packet(self, packet):
        """
        Send packet to server.

        Args:
            packet: Dictionary to send as JSON
        """
        if not self.connected or not self.ws or not self.loop:
            return False

        if not self._validate_outgoing_packet(packet):
            return False

        try:
            message = json.dumps(packet)

            # Schedule send in the async loop
            asyncio.run_coroutine_threadsafe(self.ws.send(message), self.loop)
            return True
        except Exception:
            import traceback

            traceback.print_exc()
            self.connected = False
            wx.CallAfter(self.main_window.on_connection_lost)
            return False

    def _handle_packet(self, packet):
        """
        Handle incoming packet from server (called in main thread).

        Args:
            packet: Dictionary received from server
        """
        if not self._validate_incoming_packet(packet):
            return

        packet_type = packet.get("type")

        if packet_type in {"authorize_success", "refresh_session_success"}:
            self._handle_authorize_success(packet, packet_type)
            return
        if packet_type == "refresh_session_failure":
            self._handle_refresh_failure(packet)
            return
        if packet_type == "speak":
            self.main_window.on_server_speak(packet)
            return
        if packet_type in _PACKET_DISPATCH:
            _PACKET_DISPATCH[packet_type](self.main_window, packet)

    def _handle_authorize_success(self, packet, packet_type: str) -> None:
        session_token = packet.get("session_token")
        if session_token:
            self.session_token = session_token
        session_expires_at = packet.get("session_expires_at")
        if session_expires_at:
            self.session_expires_at = session_expires_at
        refresh_token = packet.get("refresh_token")
        if refresh_token:
            self.refresh_token = refresh_token
        refresh_expires_at = packet.get("refresh_expires_at")
        if refresh_expires_at:
            self.refresh_expires_at = refresh_expires_at
        self.main_window.on_authorize_success(packet)

    def _handle_refresh_failure(self, packet) -> None:
        self.session_token = None
        self.session_expires_at = None
        self.refresh_token = None
        self.refresh_expires_at = None
        message = packet.get("message", "Session expired. Please log in again.")
        wx.CallAfter(self.main_window.add_history, message, "activity")


_PACKET_DISPATCH = {
    "play_sound": lambda window, pkt: window.on_server_play_sound(pkt),
    "play_music": lambda window, pkt: window.on_server_play_music(pkt),
    "play_ambience": lambda window, pkt: window.on_server_play_ambience(pkt),
    "stop_ambience": lambda window, pkt: window.on_server_stop_ambience(pkt),
    "add_playlist": lambda window, pkt: window.on_server_add_playlist(pkt),
    "start_playlist": lambda window, pkt: window.on_server_start_playlist(pkt),
    "remove_playlist": lambda window, pkt: window.on_server_remove_playlist(pkt),
    "get_playlist_duration": lambda window, pkt: window.on_server_get_playlist_duration(pkt),
    "menu": lambda window, pkt: window.on_server_menu(pkt),
    "request_input": lambda window, pkt: window.on_server_request_input(pkt),
    "document_editor": lambda window, pkt: window.on_server_document_editor(pkt),
    "clear_ui": lambda window, pkt: window.on_server_clear_ui(pkt),
    "game_list": lambda window, pkt: window.on_server_game_list(pkt),
    "disconnect": lambda window, pkt: window.on_server_disconnect(pkt),
    "registration_required": lambda window, pkt: window.on_registration_required(pkt),
    "email_verification_required": lambda window, pkt: window.on_email_verification_required(pkt),
    "update_options_lists": lambda window, pkt: window.on_update_options_lists(pkt),
    "open_client_options": lambda window, pkt: window.on_open_client_options(pkt),
    "open_server_options": lambda window, pkt: window.on_open_server_options(pkt),
    "open_account_settings": lambda window, pkt: window.on_open_account_settings(pkt),
    "email_change_pending": lambda window, pkt: window.on_email_change_pending(pkt),
    "email_change_complete": lambda window, pkt: window.on_email_change_complete(pkt),
    "email_change_failed": lambda window, pkt: window.on_email_change_failed(pkt),
    "table_create": lambda window, pkt: window.on_table_create(pkt),
    "pong": lambda window, pkt: window.on_server_pong(pkt),
    "chat": lambda window, pkt: window.on_receive_chat(pkt),
    "server_status": lambda window, pkt: window.on_server_status(pkt),
}
