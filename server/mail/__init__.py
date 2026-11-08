"""Email delivery for account verification and password recovery."""

from .smtp import EmailService, SMTPConfig

__all__ = ["EmailService", "SMTPConfig"]
