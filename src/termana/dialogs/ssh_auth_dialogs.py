"""Small, explicit dialogs used by the OpenSSH AskPass bridge."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Final

from PySide6.QtWidgets import QDialog, QWidget

from termana.dialogs.action_dialog import ActionDialog
from termana.i18n import tr
from termana.qt_lifecycle import schedule_delete_later

_DIRECTIONAL_FORMATS: Final = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)


def _safe_display_text(value: str) -> str:
    """Make an untrusted OpenSSH label visibly inert without changing protocol data."""
    return "".join(
        "�"
        if unicodedata.category(character).startswith("C")
        or unicodedata.bidirectional(character) in _DIRECTIONAL_FORMATS
        else character
        for character in value
    )


def confirm_ssh_action(
    *,
    endpoint: str,
    prompt: str,
    parent: QWidget | None,
    dialog_created: Callable[[QDialog], bool] | None = None,
) -> bool:
    """Present a plain-text OpenSSH confirmation with a safe default."""
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise ValueError("SSH endpoint must be non-empty trimmed text")
    if not isinstance(prompt, str):
        raise ValueError("OpenSSH prompt must be text")
    dialog = ActionDialog(
        object_name="sshAskpassConfirmationDialog",
        window_title=tr("Confirm SSH action"),
        title=tr("OpenSSH needs your confirmation"),
        subtitle=tr("SSH · {p0}", p0=f"{_safe_display_text(endpoint)}"),
        body=_safe_display_text(prompt),
        primary_text=tr("Continue"),
        primary_object_name="confirmSshActionButton",
        secondary_text=tr("Cancel"),
        secondary_object_name="cancelSshActionButton",
        tone="notice",
        badge_text="?",
        parent=parent,
    )
    try:
        if dialog_created is not None and not dialog_created(dialog):
            return False
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)


def confirm_save_ssh_password(
    *,
    endpoint: str,
    username: str,
    parent: QWidget | None,
    dialog_created: Callable[[QDialog], bool] | None = None,
) -> bool:
    """Ask for explicit consent only after OpenSSH confirmed password auth."""
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise ValueError("SSH endpoint must be non-empty trimmed text")
    if not isinstance(username, str) or not username or username != username.strip():
        raise ValueError("SSH username must be non-empty trimmed text")
    dialog = ActionDialog(
        object_name="saveSshPasswordDialog",
        window_title=tr("Save SSH password?"),
        title=tr("Save this SSH password?"),
        subtitle=(
            tr(
                "Signed in as {p0} · {p1}",
                p0=f"{_safe_display_text(username)}",
                p1=f"{_safe_display_text(endpoint)}",
            )
        ),
        body=(
            tr(
                "Store this password in your system credential store? This login will be offered the next time you connect."
            )
        ),
        primary_text=tr("Save password"),
        primary_object_name="saveSshPasswordButton",
        secondary_text=tr("Not now"),
        secondary_object_name="declineSaveSshPasswordButton",
        tone="secure",
        badge_text="✓",
        parent=parent,
    )
    try:
        if dialog_created is not None and not dialog_created(dialog):
            return False
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)


def confirm_saved_gateway_password(*, endpoint, username, parent, dialog_created=None):
    """An explicit gateway credential choice; no password is read by this dialog."""
    dialog = ActionDialog(
        object_name="savedGatewayPasswordDialog",
        window_title=tr("SSH gateway login"),
        title=tr("Use the saved gateway login?"),
        subtitle=f"{_safe_display_text(username)} · {_safe_display_text(endpoint)}",
        body=tr("This login belongs to the gateway, not the destination server."),
        primary_text=tr("Use saved password"),
        primary_object_name="useSavedGatewayPasswordButton",
        secondary_text=tr("Enter in terminal"),
        secondary_object_name="manualGatewayPasswordButton",
        tone="secure",
        badge_text="↗",
        parent=parent,
    )
    try:
        if dialog_created is not None and not dialog_created(dialog):
            return False
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        schedule_delete_later(dialog)
