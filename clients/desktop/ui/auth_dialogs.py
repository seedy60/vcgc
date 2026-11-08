"""Dialogs for SMTP-backed account verification and password recovery."""

from __future__ import annotations

import re
import ssl

import wx

from auth_flow_client import AuthFlowError, AuthFlowSession


EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def build_ssl_context(config_manager, server_id: str | None) -> ssl.SSLContext | None:
    """Build a pinned certificate context for a previously trusted server.

    A normal login stores the certificate fingerprint and PEM. Pre-login
    account flows cannot show the trust prompt, so they verify against that
    stored certificate when one exists.
    """
    if not config_manager or not server_id:
        return None
    server = config_manager.get_server_by_id(server_id)
    if not server:
        return None
    entry = server.get("trusted_certificate") or {}
    pem = entry.get("pem")
    if not pem:
        return None
    try:
        context = ssl.create_default_context(cadata=pem)
    except (ssl.SSLError, ValueError):
        return None
    # The certificate is already pinned to the server profile.
    context.check_hostname = False
    return context


def _show_error(parent, title: str, message: str) -> None:
    """Show a blocking error without leaking transport details."""
    wx.MessageBox(message, title, wx.OK | wx.ICON_ERROR)


class AccountEmailDialog(wx.Dialog):
    """Add or change the signed-in account's email address."""

    def __init__(self, parent, packet: dict, *, send_packet, on_closed=None):
        """Initialize the account email settings dialog."""
        super().__init__(parent, title="Account Settings", size=(540, 440))
        self.send_packet = send_packet
        self.on_closed = on_closed
        self.username = packet.get("username", "")
        self.current_email = packet.get("email", "")
        self.available = bool(packet.get("available", False))
        self.pending_email = ""
        self._create_ui()
        self.CenterOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def _create_ui(self) -> None:
        """Build the email change form."""
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(panel, label="Account Settings")
        title_font = title.GetFont()
        title_font.PointSize += 2
        title.SetFont(title_font.Bold())
        sizer.Add(title, 0, wx.ALL, 10)

        account_label = f"Signed in as {self.username}." if self.username else ""
        if account_label:
            sizer.Add(wx.StaticText(panel, label=account_label), 0, wx.LEFT | wx.RIGHT, 10)
        self.current_email_label = wx.StaticText(
            panel,
            label=(
                f"Current email: {self.current_email}"
                if self.current_email
                else "Current email: not set"
            ),
        )
        self.current_email_label.SetName("Current email")
        sizer.Add(self.current_email_label, 0, wx.ALL, 10)

        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Enter a new email address. The server will send a confirmation "
                    "code to it. Your current address stays active until you confirm."
                ),
            ),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            10,
        )
        sizer.Add(wx.StaticText(panel, label="&New email:"), 0, wx.LEFT, 10)
        self.email_input = wx.TextCtrl(panel)
        self.email_input.SetName("New email")
        sizer.Add(self.email_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.send_btn = wx.Button(panel, label="&Send Confirmation Code")
        sizer.Add(self.send_btn, 0, wx.ALL | wx.CENTER, 10)

        sizer.Add(wx.StaticText(panel, label="&Confirmation code:"), 0, wx.LEFT, 10)
        self.code_input = wx.TextCtrl(panel)
        self.code_input.SetName("Confirmation code")
        sizer.Add(self.code_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.status_text = wx.StaticText(panel, label="")
        self.status_text.SetName("Account settings status")
        sizer.Add(self.status_text, 0, wx.ALL, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.verify_btn = wx.Button(panel, wx.ID_OK, "&Save Email")
        self.close_btn = wx.Button(panel, wx.ID_CANCEL, "&Close")
        buttons.Add(self.verify_btn, 0, wx.RIGHT, 5)
        buttons.Add(self.close_btn, 0)
        sizer.Add(buttons, 0, wx.ALL | wx.CENTER, 10)
        panel.SetSizer(sizer)

        self.send_btn.Bind(wx.EVT_BUTTON, self.on_send_code)
        self.verify_btn.Bind(wx.EVT_BUTTON, self.on_verify)
        self.close_btn.Bind(wx.EVT_BUTTON, lambda event: self.Close())
        if self.available:
            self.email_input.SetFocus()
        else:
            self.send_btn.Disable()
            self.verify_btn.Disable()
            self.email_input.Disable()
            self.code_input.Disable()
            self._set_status(
                "Email settings are not available on this server.", is_error=True
            )

    def _set_status(self, message: str, *, is_error: bool = False) -> None:
        """Update the visible status message."""
        self.status_text.SetLabel(message or "")
        colour = (
            wx.Colour(180, 0, 0)
            if is_error
            else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        )
        self.status_text.SetForegroundColour(colour)
        self.Layout()

    def on_key(self, event) -> None:
        """Close on Escape."""
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.Close()
        else:
            event.Skip()

    def on_close(self, event) -> None:
        """Notify the owner before the dialog is destroyed."""
        if self.on_closed:
            self.on_closed(self)
        event.Skip()

    def on_send_code(self, event) -> None:
        """Request a confirmation code for the entered address."""
        email = self.email_input.GetValue().strip().lower()
        if not EMAIL_PATTERN.fullmatch(email):
            self._set_status("Enter a valid email address.", is_error=True)
            self.email_input.SetFocus()
            return
        if not self.send_packet({"type": "request_email_change", "email": email}):
            self._set_status("Not connected to the server.", is_error=True)
            return
        self._set_status("Sending a confirmation code...")

    def on_verify(self, event) -> None:
        """Confirm the pending email change with the entered code."""
        code = self.code_input.GetValue().strip()
        if not code:
            self._set_status("Enter the confirmation code.", is_error=True)
            self.code_input.SetFocus()
            return
        if not self.send_packet({"type": "verify_email_change", "code": code}):
            self._set_status("Not connected to the server.", is_error=True)
            return
        self._set_status("Confirming the new email address...")

    def apply_server_packet(self, packet: dict) -> None:
        """Apply a pending, complete, or failed response from the server."""
        packet_type = packet.get("type")
        if packet_type == "email_change_pending":
            self.pending_email = packet.get("email", "")
            self._set_status(packet.get("message", "Confirmation code sent."))
            self.code_input.SetFocus()
        elif packet_type == "email_change_complete":
            self.current_email = packet.get("email", self.pending_email)
            self.pending_email = ""
            self.current_email_label.SetLabel(f"Current email: {self.current_email}")
            self.email_input.SetValue("")
            self.code_input.SetValue("")
            self._set_status(packet.get("message", "Email address updated."))
        elif packet_type == "email_change_failed":
            self._set_status(packet.get("message", "Email change failed."), is_error=True)


class RegistrationDialog(wx.Dialog):
    """Create an account and verify its email address in one flow."""

    def __init__(
        self,
        parent,
        *,
        server_url: str,
        server_name: str = "",
        ssl_context: ssl.SSLContext | None = None,
    ):
        """Initialize the registration dialog."""
        title = f"Create Account — {server_name}" if server_name else "Create Account"
        super().__init__(parent, title=title, size=(520, 430))
        self.server_url = server_url
        self.server_name = server_name
        self.ssl_context = ssl_context
        self._session: AuthFlowSession | None = None
        self.pending: dict | None = None
        self.created_username = ""
        self.created_email = ""
        self.created_password = ""
        self._create_ui()
        self.CenterOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def _create_ui(self) -> None:
        """Build the registration form and verification step."""
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(panel, label="Create Account")
        title_font = title.GetFont()
        title_font.PointSize += 2
        title.SetFont(title_font.Bold())
        outer.Add(title, 0, wx.ALL, 10)

        self.instructions = wx.StaticText(
            panel,
            label=(
                "Enter an email address. The server will send a one-time code "
                "to verify the account before you can log in."
            ),
        )
        outer.Add(self.instructions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.form_panel = wx.Panel(panel)
        form = wx.BoxSizer(wx.VERTICAL)

        self.username_input = self._add_field(form, self.form_panel, "&Username:")
        self.email_input = self._add_field(form, self.form_panel, "&Email:")
        self.password_input = self._add_field(
            form, self.form_panel, "&Password:", password=True
        )
        self.confirm_input = self._add_field(
            form, self.form_panel, "&Confirm password:", password=True
        )

        self.register_btn = wx.Button(self.form_panel, label="Create &Account")
        self.register_btn.SetDefault()
        form.Add(self.register_btn, 0, wx.ALL | wx.CENTER, 10)
        self.form_panel.SetSizer(form)
        outer.Add(self.form_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.verify_panel = wx.Panel(panel)
        verify = wx.BoxSizer(wx.VERTICAL)
        self.pending_label = wx.StaticText(self.verify_panel, label="")
        self.pending_label.SetName("Verification status")
        verify.Add(self.pending_label, 0, wx.ALL, 10)
        self.code_input = self._add_field(
            verify, self.verify_panel, "&Verification code:"
        )
        self.verify_btn = wx.Button(self.verify_panel, label="&Verify Email")
        self.verify_btn.SetDefault()
        verify.Add(self.verify_btn, 0, wx.ALL | wx.CENTER, 10)
        self.resend_btn = wx.Button(self.verify_panel, label="&Resend Code")
        verify.Add(self.resend_btn, 0, wx.ALL | wx.CENTER, 10)
        self.verify_panel.SetSizer(verify)
        self.verify_panel.Hide()
        outer.Add(self.verify_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        self.cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        outer.Add(self.cancel_btn, 0, wx.ALL | wx.CENTER, 10)
        panel.SetSizer(outer)

        self.register_btn.Bind(wx.EVT_BUTTON, self.on_register)
        self.verify_btn.Bind(wx.EVT_BUTTON, self.on_verify)
        self.resend_btn.Bind(wx.EVT_BUTTON, self.on_resend)
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.username_input.SetFocus()

    @staticmethod
    def _add_field(sizer, parent, label: str, *, password: bool = False):
        """Add a labeled text field and return the control."""
        sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.TOP, 10)
        control = wx.TextCtrl(parent, style=wx.TE_PASSWORD if password else 0)
        if password:
            control.SetName(label.replace("&", "").rstrip(":"))
        sizer.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        return control

    def on_key(self, event) -> None:
        """Close on Escape."""
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.on_cancel(None)
        else:
            event.Skip()

    def on_close(self, event) -> None:
        """Release the short-lived connection before closing."""
        if self._session:
            self._session.close()
            self._session = None
        event.Skip()

    def _validate_form(self) -> bool:
        """Validate username, email, and password fields."""
        username = self.username_input.GetValue().strip()
        email = self.email_input.GetValue().strip().lower()
        password = self.password_input.GetValue()
        confirm = self.confirm_input.GetValue()
        if not username:
            wx.MessageBox("Username cannot be empty.", "Validation Error", wx.OK | wx.ICON_ERROR)
            self.username_input.SetFocus()
            return False
        if not EMAIL_PATTERN.fullmatch(email):
            wx.MessageBox("Enter a valid email address.", "Validation Error", wx.OK | wx.ICON_ERROR)
            self.email_input.SetFocus()
            return False
        if not password:
            wx.MessageBox("Password cannot be empty.", "Validation Error", wx.OK | wx.ICON_ERROR)
            self.password_input.SetFocus()
            return False
        if password != confirm:
            wx.MessageBox("The passwords do not match.", "Validation Error", wx.OK | wx.ICON_ERROR)
            self.confirm_input.SetFocus()
            return False
        return True

    def on_register(self, event) -> None:
        """Create the account and request a verification email."""
        if not self._validate_form():
            return
        username = self.username_input.GetValue().strip()
        email = self.email_input.GetValue().strip().lower()
        password = self.password_input.GetValue()
        try:
            if self._session:
                self._session.close()
            self._session = AuthFlowSession(self.server_url, ssl_context=self.ssl_context)
            self._session.send(
                {
                    "type": "register",
                    "username": username,
                    "password": password,
                    "email": email,
                }
            )
            response = self._session.receive(
                {"registration_pending", "registration_error"}
            )
        except AuthFlowError as exc:
            _show_error(self, "Create Account", str(exc))
            return
        if response.get("type") == "registration_error":
            _show_error(self, "Create Account", response.get("message", "Registration failed."))
            return

        self.pending = response
        self.created_username = username
        self.created_email = email
        self.created_password = password
        self.pending_label.SetLabel(response.get("message", "Enter the verification code."))
        self.form_panel.Hide()
        self.verify_panel.Show()
        self.Layout()
        self.code_input.SetFocus()

    def on_verify(self, event) -> None:
        """Verify the emailed code."""
        code = self.code_input.GetValue().strip()
        if not code:
            self.code_input.SetFocus()
            return
        if not self._session:
            _show_error(self, "Verify Email", "The verification session has expired.")
            return
        try:
            self._session.send(
                {"type": "verify_email", "username": self.created_username, "code": code}
            )
            response = self._session.receive(
                {"email_verified", "email_verification_failed"}
            )
        except AuthFlowError as exc:
            _show_error(self, "Verify Email", str(exc))
            return
        if response.get("type") == "email_verified":
            self._session.close()
            self._session = None
            self.EndModal(wx.ID_OK)
        else:
            _show_error(self, "Verify Email", response.get("message", "Verification failed."))
            self.code_input.SetFocus()

    def on_resend(self, event) -> None:
        """Request another verification code."""
        if not self._session:
            return
        try:
            self._session.send(
                {"type": "resend_verification", "username": self.created_username}
            )
            response = self._session.receive({"verification_resent"})
            self.pending_label.SetLabel(
                response.get("message", "A new code has been sent.")
            )
        except AuthFlowError as exc:
            _show_error(self, "Resend Code", str(exc))

    def on_cancel(self, event) -> None:
        """Cancel registration and close the connection."""
        if self._session:
            self._session.close()
            self._session = None
        self.EndModal(wx.ID_CANCEL)

    def get_account(self) -> dict:
        """Return the created account details after successful verification."""
        return {
            "username": self.created_username,
            "email": self.created_email,
            "password": self.created_password,
        }


class EmailVerificationDialog(wx.Dialog):
    """Verify an existing account's email address."""

    def __init__(
        self,
        parent,
        *,
        server_url: str,
        username: str,
        server_name: str = "",
        ssl_context: ssl.SSLContext | None = None,
    ):
        """Initialize the verification dialog."""
        title = f"Verify Email — {server_name}" if server_name else "Verify Email"
        super().__init__(parent, title=title, size=(500, 300))
        self.server_url = server_url
        self.username = username
        self.server_name = server_name
        self.ssl_context = ssl_context
        self._create_ui()
        self.CenterOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)

    def _create_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(panel, label=f"Verify email for {self.username}")
        title_font = title.GetFont()
        title_font.PointSize += 2
        title.SetFont(title_font.Bold())
        sizer.Add(title, 0, wx.ALL, 10)
        sizer.Add(
            wx.StaticText(
                panel,
                label="Enter the one-time code sent to the account's email address.",
            ),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            10,
        )
        sizer.Add(wx.StaticText(panel, label="&Verification code:"), 0, wx.LEFT, 10)
        self.code_input = wx.TextCtrl(panel)
        sizer.Add(self.code_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.status_text = wx.StaticText(panel, label="")
        sizer.Add(self.status_text, 0, wx.ALL, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.verify_btn = wx.Button(panel, wx.ID_OK, "&Verify")
        self.resend_btn = wx.Button(panel, label="&Resend Code")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        buttons.Add(self.verify_btn, 0, wx.RIGHT, 5)
        buttons.Add(self.resend_btn, 0, wx.RIGHT, 5)
        buttons.Add(cancel_btn, 0)
        sizer.Add(buttons, 0, wx.ALL | wx.CENTER, 10)
        panel.SetSizer(sizer)

        self.verify_btn.Bind(wx.EVT_BUTTON, self.on_verify)
        self.resend_btn.Bind(wx.EVT_BUTTON, self.on_resend)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CANCEL))
        self.code_input.SetFocus()

    def on_key(self, event) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
        else:
            event.Skip()

    def on_verify(self, event) -> None:
        code = self.code_input.GetValue().strip()
        if not code:
            self.code_input.SetFocus()
            return
        try:
            with AuthFlowSession(self.server_url, ssl_context=self.ssl_context) as session:
                session.send(
                    {"type": "verify_email", "username": self.username, "code": code}
                )
                response = session.receive(
                    {"email_verified", "email_verification_failed"}
                )
        except AuthFlowError as exc:
            _show_error(self, "Verify Email", str(exc))
            return
        if response.get("type") == "email_verified":
            wx.MessageBox(
                response.get("message", "Email verified."),
                "Verify Email",
                wx.OK | wx.ICON_INFORMATION,
            )
            self.EndModal(wx.ID_OK)
        else:
            _show_error(self, "Verify Email", response.get("message", "Verification failed."))

    def on_resend(self, event) -> None:
        try:
            with AuthFlowSession(self.server_url, ssl_context=self.ssl_context) as session:
                session.send(
                    {"type": "resend_verification", "username": self.username}
                )
                response = session.receive({"verification_resent"})
        except AuthFlowError as exc:
            _show_error(self, "Resend Code", str(exc))
            return
        self.status_text.SetLabel(response.get("message", "A new code has been sent."))
        self.Layout()


class PasswordRecoveryDialog(wx.Dialog):
    """Request an emailed reset code and set a new password."""

    def __init__(
        self,
        parent,
        *,
        server_url: str = "",
        username: str = "",
        server_name: str = "",
        ssl_context: ssl.SSLContext | None = None,
    ):
        """Initialize the recovery dialog."""
        title = f"Password Recovery — {server_name}" if server_name else "Password Recovery"
        super().__init__(parent, title=title, size=(540, 480))
        self.server_url = server_url
        self.username = (username or "").strip()
        self.server_name = server_name
        self.ssl_context = ssl_context
        self.reset_username = ""
        self.new_password = ""
        self._create_ui()
        self.CenterOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)

    def _create_ui(self) -> None:
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(panel, label="Password Recovery")
        title_font = title.GetFont()
        title_font.PointSize += 2
        title.SetFont(title_font.Bold())
        sizer.Add(title, 0, wx.ALL, 10)
        sizer.Add(
            wx.StaticText(
                panel,
                label=(
                    "Enter your username. If the account has an email address, "
                    "the server will send a one-time reset code."
                ),
            ),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            10,
        )
        sizer.Add(wx.StaticText(panel, label="&Username:"), 0, wx.LEFT, 10)
        self.username_input = wx.TextCtrl(panel, value=self.username)
        sizer.Add(self.username_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        self.request_btn = wx.Button(panel, label="&Send Reset Code")
        sizer.Add(self.request_btn, 0, wx.ALL | wx.CENTER, 10)
        self.status_text = wx.StaticText(panel, label="")
        self.status_text.SetName("Recovery status")
        sizer.Add(self.status_text, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        sizer.Add(wx.StaticText(panel, label="&Reset code:"), 0, wx.LEFT, 10)
        self.code_input = wx.TextCtrl(panel)
        sizer.Add(self.code_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(wx.StaticText(panel, label="&New password:"), 0, wx.LEFT | wx.TOP, 10)
        self.password_input = wx.TextCtrl(panel, style=wx.TE_PASSWORD)
        self.password_input.SetName("New password")
        sizer.Add(self.password_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        sizer.Add(wx.StaticText(panel, label="&Confirm new password:"), 0, wx.LEFT | wx.TOP, 10)
        self.confirm_input = wx.TextCtrl(panel, style=wx.TE_PASSWORD)
        self.confirm_input.SetName("Confirm new password")
        sizer.Add(self.confirm_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.reset_btn = wx.Button(panel, wx.ID_OK, "&Reset Password")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        buttons.Add(self.reset_btn, 0, wx.RIGHT, 5)
        buttons.Add(cancel_btn, 0)
        sizer.Add(buttons, 0, wx.ALL | wx.CENTER, 10)
        panel.SetSizer(sizer)

        self.request_btn.Bind(wx.EVT_BUTTON, self.on_request)
        self.reset_btn.Bind(wx.EVT_BUTTON, self.on_reset)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CANCEL))
        self.username_input.SetFocus()

    def on_key(self, event) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
        else:
            event.Skip()

    def _get_username(self) -> str:
        return self.username_input.GetValue().strip()

    def on_request(self, event) -> None:
        username = self._get_username()
        if not username:
            self.username_input.SetFocus()
            return
        if not self.server_url:
            _show_error(self, "Password Recovery", "No server is selected.")
            return
        try:
            with AuthFlowSession(self.server_url, ssl_context=self.ssl_context) as session:
                session.send({"type": "request_password_reset", "username": username})
                response = session.receive({"password_reset_requested"})
        except AuthFlowError as exc:
            _show_error(self, "Password Recovery", str(exc))
            return
        self.status_text.SetLabel(response.get("message", "Check your email for a reset code."))
        self.Layout()
        self.code_input.SetFocus()

    def on_reset(self, event) -> None:
        username = self._get_username()
        code = self.code_input.GetValue().strip()
        password = self.password_input.GetValue()
        confirm = self.confirm_input.GetValue()
        if not username or not code:
            _show_error(self, "Password Recovery", "Enter your username and reset code.")
            return
        if not password:
            _show_error(self, "Password Recovery", "Enter a new password.")
            self.password_input.SetFocus()
            return
        if password != confirm:
            _show_error(self, "Password Recovery", "The passwords do not match.")
            self.confirm_input.SetFocus()
            return
        if not self.server_url:
            _show_error(self, "Password Recovery", "No server is selected.")
            return
        try:
            with AuthFlowSession(self.server_url, ssl_context=self.ssl_context) as session:
                session.send(
                    {
                        "type": "reset_password",
                        "username": username,
                        "code": code,
                        "new_password": password,
                    }
                )
                response = session.receive(
                    {"password_reset_complete", "password_reset_failed"}
                )
        except AuthFlowError as exc:
            _show_error(self, "Password Recovery", str(exc))
            return
        if response.get("type") == "password_reset_complete":
            self.reset_username = username
            self.new_password = password
            wx.MessageBox(
                response.get("message", "Password reset."),
                "Password Recovery",
                wx.OK | wx.ICON_INFORMATION,
            )
            self.EndModal(wx.ID_OK)
        else:
            _show_error(self, "Password Recovery", response.get("message", "Reset failed."))
