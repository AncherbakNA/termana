"""Common saved-profile editor and verified RDP launch."""

from PySide6.QtWidgets import QMessageBox

from termana.i18n import tr
from termana.models.connection import ConnectionProtocol
from termana.rdp.component import verified_component


def rdp_component_error() -> str | None:
    try:
        verified_component()
    except (OSError, RuntimeError):
        return tr(
            "The verified RDP component is not available for this platform or build. "
            "SSH and Telnet remain available."
        )
    return None


def open_rdp_dialog(window):
    """Keep the shortcut, but use the same New connection protocol picker."""
    if window.terminal_workspace.accepts_new_terminals:
        window._connection_profile_editor.create(initial_protocol="rdp")


def open_saved_rdp_profile(window, profile):
    if (profile.protocol is not ConnectionProtocol.RDP
            or window._close_waiting_for_terminals
            or not window.terminal_workspace.accepts_new_terminals):
        return False
    error = rdp_component_error()
    if error is not None:
        QMessageBox.warning(window, tr("RDP component unavailable"), error)
        return False
    options = profile.rdp_options
    repository = getattr(window,"_rdp_identity_repository",None)
    launch = dict(display_size=(options.width, options.height),profile=profile)
    if repository is not None:
        launch["page_factory"] = repository.page_factory
    return window.terminal_workspace.open_rdp_session(
        profile.host, profile.port, **launch,
    ) is not None
