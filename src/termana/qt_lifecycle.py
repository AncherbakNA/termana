"""Small lifecycle helpers for temporary Qt objects."""


def schedule_delete_later(qt_object: object) -> None:
    """Schedule Qt-owned cleanup while remaining friendly to lightweight test doubles."""
    delete_later = getattr(qt_object, "deleteLater", None)
    if callable(delete_later):
        delete_later()
