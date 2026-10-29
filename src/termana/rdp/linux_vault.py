"""RDP-only policy over the verified native Secret Service backend.

The library's default collection helper may create a collection and wait for a
master-password dialog. RDP never configures the user's keyring or unlocks it as
a side effect of reading/saving a sign-in. Manual RDP sign-in remains available.
"""

from importlib import import_module

from termana.credentials.secret_store import SecretStoreUnavailableError

_UNAVAILABLE = "RDP sign-in storage requires an existing unlocked system keyring"


def existing_collection_backend(backend):
    """Adapt only the exact backend already selected by the native allowlist."""
    base = type(backend)
    if base.__module__ != "keyring.backends.SecretService" or base.__name__ != "Keyring":
        raise SecretStoreUnavailableError("The native RDP keyring backend is not supported")
    storage = import_module("secretstorage")

    class ExistingCollectionOnly(base):
        # This is a per-RDP instance policy, not a library/global monkeypatch.
        # Inherited query/encryption/item operations stay with the native library.
        def get_preferred_collection(self):
            connection = None
            try:
                connection = storage.dbus_init()
                # Collection() refuses a missing alias. get_default_collection()
                # must not be used here: it implicitly creates the collection.
                collection = storage.collection.Collection(connection)
                if collection.is_locked():
                    raise SecretStoreUnavailableError(_UNAVAILABLE)
                return collection
            except Exception:
                if connection is not None:
                    connection.close()
                raise SecretStoreUnavailableError(_UNAVAILABLE) from None

        def unlock(self, item):
            if item.is_locked():
                raise SecretStoreUnavailableError(_UNAVAILABLE)

    return ExistingCollectionOnly()
