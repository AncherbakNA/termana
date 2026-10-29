"""Per-launch insecure-protocol consent, with a live source-owner guard."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog

from termana.dialogs.action_dialog import ActionDialog
from termana.i18n import tr


def confirm_legacy_ssh(profile, parent, owner_live):
    if not owner_live():
        return False
    message = ActionDialog(
        object_name="legacySshProtocolConsentDialog",
        window_title=tr("Insecure legacy SSH1"),
        title=tr("Connect using insecure SSH1?"),
        subtitle=f"{profile.name} · {profile.host}:{profile.port}",
        body=tr("SSH1 is insecure. Use it only for legacy devices on trusted isolated networks; prefer SSH2.\n\nUses a separate Termana component: selected RSA1 keys/passwords only. No protocol fallback, SSH config, agent, SFTP, gateway or tunnels.\n\nServer-key approval is required before password access. This approval lasts for this launch only."),
        primary_text=tr("Connect with SSH1"), primary_object_name="connectLegacySshButton",
        secondary_text=tr("Cancel"), secondary_object_name="cancelLegacySshButton",
        primary_role="danger", tone="notice", badge_text="!", parent=parent,
    )
    timer = QTimer(message)
    timer.setInterval(50)
    timer.timeout.connect(lambda: None if owner_live() else message.reject())
    timer.start()
    try:
        return message.exec() == QDialog.DialogCode.Accepted and owner_live()
    finally:
        timer.stop()
        message.deleteLater()
