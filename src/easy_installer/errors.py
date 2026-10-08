"""Exception hierarchy. ``str(exc)`` is always a translated, user-facing sentence."""

from __future__ import annotations


class EasyInstallerError(Exception):
    """Base class. ``message`` is shown to the user, ``details`` is optional technical text."""

    def __init__(self, message: str, details: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        return self.message


class NotAnAppImageError(EasyInstallerError):
    """The file is not an AppImage (not ELF, no payload, unsupported type)."""


class ExtractionError(EasyInstallerError):
    """Reading the AppImage payload failed (damaged or incomplete download)."""


class UnsupportedArchitectureError(EasyInstallerError):
    """The AppImage was built for a different CPU architecture."""


class InstallError(EasyInstallerError):
    """Installing or uninstalling failed (disk full, permissions, I/O)."""


class NotInstalledError(EasyInstallerError):
    """No installed app with the requested id."""


class AuthorizationError(EasyInstallerError):
    """The administrator password prompt was dismissed or authentication failed."""


class HelperError(EasyInstallerError):
    """The privileged helper reported an error or crashed."""


class NetworkError(EasyInstallerError):
    """The internet could not be reached, or a server answered with an error."""


class UpdateError(EasyInstallerError):
    """Checking for or applying an update failed (bad download, wrong app, checksum mismatch)."""


class UpdateCancelled(EasyInstallerError):
    """The user cancelled a running download or update."""


class ArchiveError(EasyInstallerError):
    """A portable app archive (.tar.gz, .zip, ...) is damaged, unsafe or not an app."""
