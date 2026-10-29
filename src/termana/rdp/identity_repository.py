"""Application-owned vault jobs and profile revocation, independent of page life."""
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from termana.credentials.background_operation import BackgroundOperation
from termana.i18n import tr
from termana.models.connection import ConnectionProtocol


class _Completion(QObject):
    finished = Signal(object)


class _TrackedTask(QRunnable):
    def __init__(self, operation, task):
        super().__init__()
        self.operation, self.task = operation, task
        self.signals = _Completion()

    def run(self):
        try:
            self.task.run()
        finally:
            # ConfidentialBackgroundOperation deliberately emits no successful
            # result for an abandoned task, including abandonment while native
            # I/O is running. Repository lifetime must track actual return, not
            # those UI-result signals, or application shutdown can wait forever.
            self.signals.finished.emit(self.operation)


class RdpIdentityRepository(QObject):
    pending_count_changed = Signal(int)
    operation_finished = Signal(object)
    message = Signal(str)

    def __init__(self, vault, trust_store, parent=None, *, starter=None):
        super().__init__(parent)
        self.vault, self.trust_store = vault, trust_store
        self._tasks = {}
        self._starter = starter or QThreadPool.globalInstance().start

    @property
    def is_busy(self):
        return bool(self._tasks)

    def start(self, operation, task):
        if operation in self._tasks:
            raise ValueError("Duplicate RDP identity operation")
        wrapper = _TrackedTask(operation,task)
        self._tasks[operation] = wrapper
        wrapper.signals.finished.connect(self._finished)
        self.pending_count_changed.emit(len(self._tasks))
        try:
            self._starter(wrapper)
        except Exception:
            # Both the page and repository see the same generic failure; no
            # confidential closure/result or native exception enters a signal.
            task.signals.failed.emit(operation)
            wrapper.signals.finished.emit(operation)

    @Slot(object)
    def _finished(self, operation):
        if self._tasks.pop(operation, None) is not None:
            self.operation_finished.emit(operation)
            self.pending_count_changed.emit(len(self._tasks))

    def page_factory(self, host, port, **kwargs):
        from termana.rdp.page import RdpPage
        return RdpPage(host,port,vault=self.vault,trust_store=self.trust_store,
                       identity_repository=self,**kwargs)

    def catalog_changed(self, previous, current):
        for profile in previous.profiles:
            replacement = current.profile_by_id(profile.profile_id)
            if (profile.protocol is not ConnectionProtocol.RDP or
                    (replacement is not None and replacement.protocol is ConnectionProtocol.RDP)):
                continue
            # Immediate in-memory fence. A late successful old page cannot save
            # this removed profile again. The queued native delete serializes
            # after any already-running save in the shared vault's I/O lock.
            self.vault.revoke_profile(profile)
            operation, vault = uuid4(), self.vault
            task = BackgroundOperation(operation, lambda item=profile, store=vault: store.forget_login(item))
            task.signals.failed.connect(self._delete_failed)
            self.start(operation,task)

    @Slot(object)
    def _delete_failed(self, _operation):
        self.message.emit(tr("The profile was removed, but its RDP vault entry could not be deleted. Review the system credential store."))
