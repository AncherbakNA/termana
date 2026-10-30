"""GitHub App Device Flow and single encrypted-file CAS; never use publisher keys.

Authenticated requests cannot redirect. Server bodies/errors never become public
diagnostics. User authorization is restricted by the app installation, not scopes.
"""

import base64
import hashlib
import re
import ssl
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from termana.profile_sync.crypto import MAX_ENVELOPE_BYTES
from termana.profile_sync.model import SyncError, canonical_json, parse_json
from termana.updates.manifest import ReleaseRepository

CLIENT_ID = "Iv23liuzdGYq4RNEbbFJ"
SYNC_PATH = "termana-catalog.enc"
MAX_RESPONSE = (MAX_ENVELOPE_BYTES * 3 // 2) + 131072


class RevisionConflict(SyncError):
    """Remote changed; fetch and merge again rather than retrying a stale PUT."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request_json(method, path, data=None, *, token=None, oauth=False, missing=False):
    """Fixed origins only, bounded response and timeout, no credential redirects."""
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise SyncError("Invalid GitHub request")
    origin = "https://github.com" if oauth else "https://api.github.com"
    headers = {"Accept": "application/json", "User-Agent": "Termana-Sync/1"}
    if token is not None:
        if type(token) is not str or not re.fullmatch(r"[A-Za-z0-9_]{20,512}", token):
            raise SyncError("Sign in to GitHub again")
        headers["Authorization"] = "Bearer " + token
    if oauth:
        body = None if data is None else urlencode(data).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        body = None if data is None else canonical_json(data)
        headers.update({"Content-Type": "application/json", "X-GitHub-Api-Version": "2026-03-10"})
    try:
        with build_opener(_NoRedirect()).open(
            Request(origin + path, data=body, headers=headers, method=method), timeout=15
        ) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise SyncError("GitHub response exceeded the size limit")
            return parse_json(raw, limit=MAX_RESPONSE)
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 404 and missing:
            return None
        if code in (409, 422):
            raise RevisionConflict("Remote changed; synchronize again") from None
        if code == 401:
            raise SyncError("GitHub authorization expired; sign in again") from None
        if code in (403, 429):
            raise SyncError("GitHub refused access or rate-limited sync; retry later") from None
        raise SyncError("GitHub request failed; no local connections were replaced") from None
    except SyncError:
        raise
    except (URLError, OSError, ValueError) as error:
        if isinstance(getattr(error, "reason", error), ssl.SSLCertVerificationError):
            raise SyncError(
                "GitHub's certificate could not be verified. Check your clock and system trusted certificates; TLS verification remains enabled."
            ) from None
        raise SyncError(
            "Could not securely reach GitHub; your local connections are available"
        ) from None


@dataclass(frozen=True)
class DeviceCode:
    device_code: str = field(repr=False)
    user_code: str
    interval: int
    expires_in: int


def begin_login(request=request_json):
    data = request("POST", "/login/device/code", {"client_id": CLIENT_ID}, oauth=True)
    if (
        type(data) is not dict
        or type(data.get("device_code")) is not str
        or not re.fullmatch(r"[a-fA-F0-9]{40,128}", data["device_code"])
        or type(data.get("user_code")) is not str
        or not re.fullmatch(r"[A-Z0-9-]{6,16}", data["user_code"])
        or data.get("verification_uri") != "https://github.com/login/device"
        or type(data.get("interval")) is not int
        or not 1 <= data["interval"] <= 30
        or type(data.get("expires_in")) is not int
        or not 30 <= data["expires_in"] <= 1800
    ):
        raise SyncError("GitHub did not provide a valid sign-in code")
    return DeviceCode(data["device_code"], data["user_code"], data["interval"], data["expires_in"])


def finish_login(code, cancelled, request=request_json, clock=time.monotonic):
    deadline, interval = clock() + code.expires_in, code.interval
    while clock() < deadline:
        if cancelled.wait(interval):
            raise SyncError("GitHub sign-in cancelled")
        data = request(
            "POST",
            "/login/oauth/access_token",
            {
                "client_id": CLIENT_ID,
                "device_code": code.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            oauth=True,
        )
        if cancelled.is_set():
            raise SyncError("GitHub sign-in cancelled")
        if type(data) is not dict:
            raise SyncError("Invalid GitHub sign-in response")
        if data.get("error") == "authorization_pending":
            continue
        if data.get("error") == "slow_down":
            interval += 5
            continue
        if data.get("error"):
            raise SyncError("GitHub sign-in denied or expired; start again")
        token = data.get("access_token")
        if (
            data.get("token_type") != "bearer"
            or type(token) is not str
            or not re.fullmatch(r"[A-Za-z0-9_]{20,512}", token)
        ):
            raise SyncError("Invalid GitHub authorization")
        # No app-wide client secret in desktop software. Expired tokens use a new
        # Device Flow; do not pretend a secret-dependent refresh is available.
        return token
    raise SyncError("GitHub sign-in expired; start again")


def blob_sha(payload):
    return hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


class GitHubCatalog:
    def __init__(self, repository, token, *, request=request_json):
        if type(repository) is not ReleaseRepository:
            raise SyncError("Select a private GitHub repository")
        self.repository, self._token, self._request = repository, token, request
        self.prefix = "/repos/" + repository.identity

    def validate(self, *, repository_id=None, account_id=None):
        user = self._request("GET", "/user", token=self._token)
        repo = self._request("GET", self.prefix, token=self._token)
        if (
            type(user) is not dict
            or type(repo) is not dict
            or repo.get("private") is not True
            or repo.get("archived") is not False
            or repo.get("owner", {}).get("id") != user.get("id")
            or repo.get("permissions", {}).get("push") is not True
            or any(
                type(v) is not int or not 0 < v <= 2**53 - 1
                for v in (repo.get("id"), user.get("id"))
            )
            or (repository_id is not None and repo["id"] != repository_id)
            or (account_id is not None and user["id"] != account_id)
        ):
            raise SyncError(
                "Use your own private writable repository; account or repository did not match"
            )
        return repo["id"], user["id"]

    def read(self):
        try:
            history = self._request(
                "GET", self.prefix + "/commits?path=" + SYNC_PATH + "&per_page=1", token=self._token
            )
        except RevisionConflict:
            # GitHub's commits endpoint returns409 for a brand-new empty repo.
            # Only an independently confirmed missing file may bootstrap; a PUT
            # without SHA still cannot overwrite a concurrently created file.
            if (
                self._request(
                    "GET", self.prefix + "/contents/" + SYNC_PATH, token=self._token, missing=True
                )
                is None
            ):
                return None
            raise SyncError("Could not verify the remote catalog history") from None
        if history == []:
            return None
        if (
            type(history) is not list
            or len(history) != 1
            or type(history[0]) is not dict
            or type(history[0].get("sha")) is not str
            or not re.fullmatch(r"[a-f0-9]{40}", history[0]["sha"])
        ):
            raise SyncError("Invalid GitHub catalog history")
        commit = history[0]["sha"]
        result = self._request(
            "GET",
            self.prefix + "/contents/" + SYNC_PATH + "?ref=" + commit,
            token=self._token,
            missing=True,
        )
        if result is None:
            return None
        if (
            type(result) is dict
            and result.get("type") == "file"
            and result.get("encoding") == "none"
            and type(result.get("size")) is int
            and 1 <= result["size"] <= MAX_ENVELOPE_BYTES
            and type(result.get("sha")) is str
            and re.fullmatch(r"[a-f0-9]{40}", result["sha"])
        ):
            expected_sha, expected_size = result["sha"], result["size"]
            blob = self._request(
                "GET", self.prefix + "/git/blobs/" + expected_sha, token=self._token
            )
            if (
                type(blob) is not dict
                or blob.get("sha") != expected_sha
                or blob.get("size") != expected_size
            ):
                raise SyncError("GitHub blob does not match the encrypted catalog")
            result = {"type": "file", **blob}
        try:
            if (
                type(result) is not dict
                or result.get("type") != "file"
                or result.get("encoding") != "base64"
                or type(result.get("size")) is not int
                or not 1 <= result["size"] <= MAX_ENVELOPE_BYTES
            ):
                raise ValueError()
            raw = base64.b64decode(result["content"].replace("\n", ""), validate=True)
            if len(raw) != result["size"] or blob_sha(raw) != result["sha"]:
                raise ValueError()
            return raw, result["sha"], commit
        except (ValueError, TypeError, KeyError, AttributeError):
            raise SyncError("Invalid encrypted GitHub catalog") from None

    def check_history(self, previous, current):
        if previous is None or previous == current:
            return
        if any(
            type(value) is not str or not re.fullmatch(r"[a-f0-9]{40}", value)
            for value in (previous, current)
        ):
            raise SyncError("Invalid accepted sync revision")
        result = self._request(
            "GET", self.prefix + "/compare/" + previous + "..." + current, token=self._token
        )
        if type(result) is not dict or result.get("status") not in {"ahead", "identical"}:
            raise SyncError("Remote history was rolled back or rewritten; nothing was overwritten")

    def write(self, payload, expected_sha):
        if type(payload) is not bytes or not 1 <= len(payload) <= MAX_ENVELOPE_BYTES:
            raise SyncError("Invalid encrypted catalog size")
        if expected_sha is not None and (
            type(expected_sha) is not str or not re.fullmatch(r"[a-f0-9]{40}", expected_sha)
        ):
            raise SyncError("Invalid remote revision")
        data = {
            "message": "Synchronize encrypted Termana connections",
            "content": base64.b64encode(payload).decode("ascii"),
        }
        if expected_sha is not None:
            data["sha"] = expected_sha
        result = self._request(
            "PUT", self.prefix + "/contents/" + SYNC_PATH, data, token=self._token
        )
        expected = blob_sha(payload)
        if type(result) is not dict or result.get("content", {}).get("sha") != expected:
            raise SyncError("Remote save could not be confirmed; synchronize again before editing")
        commit = result.get("commit", {}).get("sha")
        if type(commit) is not str or not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise SyncError("Remote commit could not be confirmed; synchronize again")
        return commit
