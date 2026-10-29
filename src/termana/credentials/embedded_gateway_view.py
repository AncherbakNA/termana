"""GUI-owned role view of one gateway; no worker, transport or credential clone."""

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from termana.terminal.embedded_ssh import EmbeddedSshPlan
from termana.terminal.embedded_ssh_route import gateway_profile


class EmbeddedGatewayView(QObject):
    secret_requested = Signal(object)
    secret_request_finished = Signal(object)
    authenticated = Signal()

    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self._backend = backend
        self.plan = EmbeddedSshPlan(gateway_profile(backend.plan.profile), backend.plan.known_hosts)
        self.authentication_id = backend.gateway_authentication_id
        backend.secret_requested.connect(self._request)
        backend.secret_request_finished.connect(self._finished)
        backend.gateway_authenticated.connect(self.authenticated)

    @staticmethod
    def _as_password(request):
        return replace(request, is_host_password=not request.is_host_confirmation)

    def _request(self, request):
        if request.is_gateway:
            self.secret_requested.emit(self._as_password(request))

    def _finished(self, request):
        if request.is_gateway:
            self.secret_request_finished.emit(self._as_password(request))

    @property
    def current_secret_request(self):
        request = self._backend.current_secret_request
        return self._as_password(request) if request is not None and request.is_gateway else None

    @property
    def authentication_live(self):
        return self._backend.gateway_authentication_live

    @property
    def password_proof_deadline(self):
        return self._backend.gateway_password_proof_deadline

    def take_proven_password(self):
        return self._backend.take_gateway_proven_password()

    def submit_secret(self, request_id, value):
        request = self.current_secret_request
        if request is None or request.request_id != request_id:
            return False
        return self._backend.submit_secret(request_id, value)

    def close(self):
        self._backend.close()
