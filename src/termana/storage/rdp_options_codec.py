"""Strict local/portable RDP options: password/trust fields have no representation."""

from termana.models.rdp_options import RdpOptions

LEGACY_DISPLAY_FIELDS = frozenset({"width", "height", "zoom", "view_only", "full_screen"})
LEGACY_LOCAL_FIELDS = LEGACY_DISPLAY_FIELDS | {"username", "domain"}
DISPLAY_FIELDS = LEGACY_DISPLAY_FIELDS | {"dynamic_resolution", "keyboard_mode", "admin_session"}
LOCAL_FIELDS = DISPLAY_FIELDS | {"username", "domain", "clipboard_to_remote", "clipboard_to_local"}


def rdp_options_to_document(options, *, portable=False):
    if type(options) is not RdpOptions:
        raise ValueError("Expected RDP options")
    fields = DISPLAY_FIELDS if portable else LOCAL_FIELDS
    return {name: getattr(options, name) for name in sorted(fields)}


def rdp_options_from_document(document, *, portable=False, legacy=False):
    fields = ((LEGACY_DISPLAY_FIELDS if portable else LEGACY_LOCAL_FIELDS) if legacy
              else (DISPLAY_FIELDS if portable else LOCAL_FIELDS))
    if type(document) is not dict or set(document) != fields:
        raise ValueError("RDP option fields do not match the supported format")
    return RdpOptions(**document)
