"""Bounded folder planning and copying on one live SFTP owner, never shell commands.

Plans are not snapshots. Local reads/writes are directory-anchored; SFTP paths
are checked without deliberately following links. SFTP v3 cannot provide an
atomic, handle-relative remote directory sandbox or a remote content CAS.
"""

import asyncio
import os
import posixpath
import stat
import unicodedata
from contextlib import aclosing
from dataclasses import dataclass, replace

from termana.credentials.ssh_file_metadata import same_ssh_path_status
from termana.models.sftp import (
    MAX_SFTP_PATH_CHARACTERS,
    SFTP_CHUNK_BYTES,
    SftpOperation,
    SftpProgress,
    SftpRequest,
    SftpResult,
    SftpTreePolicy,
    SftpTreeSummary,
)
from termana.models.sftp_batch import local_download_name
from termana.terminal.sftp_tree_local import (
    TreeLocalChangedError,
    is_link,
    local_directory,
    same_directory,
)

MAX_TREE_ENTRIES = 10000
MAX_TREE_DEPTH = 64
MAX_TREE_NAME_BYTES = 8 * 1024 * 1024
TREE_PLAN_TIMEOUT = 120


class TreeConflictRequired(Exception):
    pass


class TreePlanError(ValueError):
    """Only constant, application-authored public reasons may be passed here."""


@dataclass
class TreeNode:
    parts: tuple[str, ...]
    directory: bool
    source: object
    size: int = 0
    destination: object | None = None


def remote_version(attrs):
    return tuple(
        getattr(attrs, field, None)
        for field in ("permissions", "size", "mtime", "mtime_ns", "ctime", "ctime_ns")
    )


class SftpTreeTransfer:
    def __init__(self, runtime, request):
        self.runtime = runtime
        self.request = request
        self.upload = request.operation is SftpOperation.UPLOAD_TREE
        self.summary = SftpTreeSummary()
        self.nodes = []
        self._identities = set()
        self._scanned = 0
        self._remote_records = 0
        self._name_bytes = 0
        self._by_parts = {}
        self._source_directories = {}
        self._source_names = {}
        self._directories_completed = 0
        # Resolve the explicitly selected parent once. Descendants never use
        # resolve()/followlinks; the root's own identity is checked separately.
        selected = request.local_path
        if os.name == "nt":
            from termana.credentials.windows_ssh_files import native_windows_ssh_file_name

            # Reject UNC/mapped network/device paths before filesystem metadata
            # or resolve() can contact a different endpoint. The directory
            # capability subsequently rejects every filesystem reparse point.
            native_windows_ssh_file_name(selected)
            self.local = selected
        else:
            self.local = selected.parent.resolve(strict=True) / selected.name

    @property
    def client(self):
        return self.runtime._client

    def check(self):
        self.runtime._check_cancel(self.request)

    def remote(self, parts):
        return posixpath.join(self.request.remote_path, *parts)

    def _record(self, parts):
        self._scanned += 1
        if self._scanned > MAX_TREE_ENTRIES or len(parts) > MAX_TREE_DEPTH:
            raise TreePlanError("Folder scan limit exceeded (10000 entries / 64 levels)")
        for part in parts[-1:]:
            try:
                local_download_name(part)
                self._name_bytes += len(part.encode("utf-8"))
            except (ValueError, UnicodeError):
                raise TreePlanError(
                    "A folder contains an unsafe or non-portable filename"
                ) from None
        if (
            self._name_bytes > MAX_TREE_NAME_BYTES
            or len(self.remote(parts)) > MAX_SFTP_PATH_CHARACTERS
            or len(str(self.local.joinpath(*parts))) > MAX_SFTP_PATH_CHARACTERS
        ):
            raise TreePlanError("Folder path or name-size limit exceeded")
        # Parents were already checked when admitted. Reuse their raw strings
        # instead of allocating a normalized full prefix for every descendant.
        identity = (
            (*parts[:-1], unicodedata.normalize("NFC", parts[-1]).casefold()) if parts else ()
        )
        if identity in self._identities:
            raise TreePlanError("Folder names collide on a case-insensitive filesystem")
        self._identities.add(identity)

    def _skip_unsupported(self):
        self.summary = replace(self.summary, skipped_links=self.summary.skipped_links + 1)

    def progress(self, transferred=0, *, planning=False, phase="transfer"):
        self.runtime._publish(
            SftpProgress(
                self.request.request_id,
                self.summary.transferred + transferred,
                None if planning else self.summary.total,
                self.summary.files_completed,
                self.summary.files_total,
                "planning" if planning else phase,
                self._directories_completed,
                len(self._source_directories),
            )
        )

    async def _guard_remote_directory(self, path):
        attrs = await self.runtime._io(self.client.lstat(path))
        canonical = await self.runtime._io(self.client.realpath(path))
        if not stat.S_ISDIR(attrs.permissions or 0) or canonical != path:
            raise TreePlanError("A remote folder changed or is a symbolic link")
        return attrs

    async def _scan_local(self):
        # Open/pin the directory before requesting metadata, including on
        # Windows where a parent junction must not trigger a path preflight.
        stack = [((), None)]
        while stack:
            self.check()
            parts, expected = stack.pop()
            with local_directory(self.local.joinpath(*parts)) as folder:
                before = folder.status()
                if expected is not None and not same_directory(expected, before):
                    raise TreeLocalChangedError()
                self._source_directories[parts] = before
                if not parts:
                    self._record(parts)
                    self.nodes.append(TreeNode(parts, True, before))
                with folder.entries() as iterator:
                    for entry in iterator:
                        self.check()
                        child = (*parts, entry.name)
                        self._record(child)
                        attrs = folder.lstat(entry.name)
                        if attrs is None:
                            raise TreeLocalChangedError()
                        if is_link(attrs) or not (
                            stat.S_ISDIR(attrs.st_mode) or stat.S_ISREG(attrs.st_mode)
                        ):
                            self._skip_unsupported()
                        elif stat.S_ISDIR(attrs.st_mode):
                            self.nodes.append(TreeNode(child, True, attrs))
                            stack.append((child, attrs))
                        else:
                            self.nodes.append(TreeNode(child, False, attrs, attrs.st_size))
                        if self._scanned % 32 == 0:
                            self.progress(planning=True)
                            await asyncio.sleep(0)
                if not same_ssh_path_status(before, folder.status()):
                    raise TreeLocalChangedError()

    async def _scan_remote(self):
        stack = [()]
        while stack:
            self.check()
            parts = stack.pop()
            attrs = await self._guard_remote_directory(self.remote(parts))
            self._source_directories[parts] = attrs
            names = self._source_names[parts] = set()
            if not parts:
                self._record(parts)
                self.nodes.append(TreeNode(parts, True, attrs))
            async with aclosing(self.client.scandir(self.remote(parts))) as iterator:
                while True:
                    self.check()
                    try:
                        entry = await self.runtime._io(anext(iterator))
                    except StopAsyncIteration:
                        break
                    # A separate wire-record budget counts even dots. The node
                    # limit remains symmetric with local scans, while an
                    # endless dot stream still stops without creating anything.
                    self._remote_records += 1
                    if self._remote_records > MAX_TREE_ENTRIES * 3:
                        raise TreePlanError("Folder scan record limit exceeded")
                    if entry.filename in (".", ".."):
                        continue
                    if type(entry.filename) is not str:
                        raise TreePlanError("A folder contains an unsupported filename")
                    child = (*parts, entry.filename)
                    self._record(child)
                    names.add(entry.filename)
                    value = await self.runtime._io(self.client.lstat(self.remote(child)))
                    mode = value.permissions or 0
                    if stat.S_ISDIR(mode):
                        self.nodes.append(TreeNode(child, True, value))
                        stack.append(child)
                    elif stat.S_ISREG(mode):
                        if type(value.size) is not int or not 0 <= value.size < 2**63:
                            raise TreePlanError("A source file has an unsupported size")
                        self.nodes.append(TreeNode(child, False, value, value.size))
                    else:
                        self._skip_unsupported()
                    if self._scanned % 32 == 0:
                        self.progress(planning=True)
            after = await self.runtime._io(self.client.lstat(self.remote(parts)))
            if remote_version(attrs) != remote_version(after):
                raise TreePlanError("The remote source folder changed during scanning")

    async def _destinations(self):
        missing = set()
        conflicts = False
        self.nodes.sort(key=lambda node: (len(node.parts), node.parts))
        self._by_parts = {node.parts: node for node in self.nodes}
        for node in self.nodes:
            self.check()
            if any(node.parts[:length] in missing for length in range(len(node.parts))):
                value = None
            elif self.upload:
                value = await self.runtime._remote_exists(self.remote(node.parts))
            else:
                path = self.local.joinpath(*node.parts)
                with local_directory(path.parent) as parent:
                    value = parent.lstat(path.name)
            node.destination = value
            mode = (value.permissions if self.upload else value.st_mode) if value else 0
            link = stat.S_ISLNK(mode or 0) if self.upload else value is not None and is_link(value)
            if value is not None and (
                link
                or (not stat.S_ISDIR(mode or 0) if node.directory else not stat.S_ISREG(mode or 0))
            ):
                raise TreePlanError(
                    "A destination entry is a link or another type; choose another folder"
                )
            if node.directory:
                if value is None:
                    missing.add(node.parts)
            elif value is not None:
                conflicts = True
        files = [node for node in self.nodes if not node.directory]
        skipped = (
            sum(node.destination is not None for node in files)
            if self.request.tree_policy is SftpTreePolicy.SKIP
            else 0
        )
        total = sum(
            node.size
            for node in files
            if not (
                node.destination is not None and self.request.tree_policy is SftpTreePolicy.SKIP
            )
        )
        if not 0 <= total < 2**63:
            raise TreePlanError("The source folder has an unsupported total size")
        self.summary = replace(
            self.summary, files_total=len(files) - skipped, skipped_existing=skipped, total=total
        )
        if conflicts and self.request.tree_policy is SftpTreePolicy.ASK:
            raise TreeConflictRequired()

    def _created(self, _path):
        self.summary = replace(
            self.summary, directories_created=self.summary.directories_created + 1
        )

    async def _directories(self):
        for node in self.nodes:
            if not node.directory:
                continue
            self.check()
            if self.upload:
                path = self.remote(node.parts)
                await self._guard_remote_directory(posixpath.dirname(path))
                current = await self.runtime._remote_exists(path)
                if current is None:
                    import asyncssh

                    await self.runtime._io(
                        self.client.mkdir(path, attrs=asyncssh.SFTPAttrs(permissions=0o700))
                    )
                    self._created(path)
                else:
                    await self._guard_remote_directory(path)
            else:
                path = self.local.joinpath(*node.parts)
                with local_directory(
                    path, creation_root=self.local, created=self._created
                ) as folder:
                    if node.destination is not None and not same_directory(
                        node.destination, folder.status()
                    ):
                        raise TreeLocalChangedError()
                    node.destination = folder.status()
            self._directories_completed += 1
            self.progress(phase="folders")

    async def _validate_remote_names(self, parts, records):
        # SFTP v3 timestamps can have only second precision and directory size
        # need not change for an added name. Re-read the bounded manifest rather
        # than treating unchanged metadata as proof of an unchanged membership.
        remaining = set(self._source_names[parts])
        async with aclosing(self.client.scandir(self.remote(parts))) as iterator:
            while True:
                self.check()
                try:
                    entry = await self.runtime._io(anext(iterator))
                except StopAsyncIteration:
                    break
                records += 1
                if records > MAX_TREE_ENTRIES * 3:
                    raise TreePlanError("Folder verification record limit exceeded")
                if entry.filename in (".", ".."):
                    continue
                if type(entry.filename) is not str or entry.filename not in remaining:
                    raise TreePlanError("A remote source folder changed; review the partial folder")
                remaining.remove(entry.filename)
        if remaining:
            raise TreePlanError("A remote source folder changed; review the partial folder")
        return records

    async def _validate_source_directories(self):
        async with asyncio.timeout(TREE_PLAN_TIMEOUT):
            await self._validate_source_directory_state()

    async def _validate_source_directory_state(self):
        records = 0
        for parts, expected in self._source_directories.items():
            self.check()
            if self.upload:
                with local_directory(self.local.joinpath(*parts)) as folder:
                    if not same_ssh_path_status(expected, folder.status()):
                        raise TreePlanError(
                            "A local source folder changed; review the partial folder"
                        )
            else:
                current = await self._guard_remote_directory(self.remote(parts))
                if remote_version(expected) != remote_version(current):
                    raise TreePlanError("A remote source folder changed; review the partial folder")
                records = await self._validate_remote_names(parts, records)

    def _local_parent_expected(self, node):
        return self._by_parts[node.parts[:-1]].destination

    async def _upload_file(self, node):
        path = self.remote(node.parts)

        async def guard():
            await self._guard_remote_directory(posixpath.dirname(path))
            current = await self.runtime._remote_exists(path)
            if (current is None) != (node.destination is None) or (
                current is not None and remote_version(current) != remote_version(node.destination)
            ):
                raise TreePlanError(
                    "A remote destination changed; review the partial folder before retrying"
                )

        await guard()
        local = self.local.joinpath(*node.parts)

        def progress(event):
            if event.transferred > node.size:
                raise TreePlanError("A local source file grew during transfer")
            self.progress(event.transferred)

        with local_directory(local.parent) as parent:
            request = SftpRequest(
                SftpOperation.UPLOAD,
                path,
                local,
                overwrite=node.destination is not None,
                request_id=self.request.request_id,
            )
            await self.runtime._upload(
                request,
                open_source=lambda _path: parent.source(local.name, node.source),
                progress=progress,
                before_commit=guard,
            )

    async def _download_file(self, node):
        path = self.remote(node.parts)
        await self._guard_remote_directory(posixpath.dirname(path))
        attrs = await self.runtime._io(self.client.lstat(path))
        if remote_version(attrs) != remote_version(node.source):
            raise TreePlanError(
                "A remote source file changed; review the partial folder before retrying"
            )
        local = self.local.joinpath(*node.parts)
        with local_directory(local.parent) as parent:
            if not same_directory(self._local_parent_expected(node), parent.status()):
                raise TreeLocalChangedError()
            await self._download_stream(node, parent)

    async def _download_stream(self, node, parent):
        path = self.remote(node.parts)
        local = self.local.joinpath(*node.parts)
        with parent.writer(local.name, node.destination) as stream:
            async with self.runtime._file(path, "rb") as source:
                opened = await self.runtime._io(source.stat())
                if remote_version(opened) != remote_version(node.source):
                    raise TreePlanError("A remote source file changed before reading")
                transferred = 0
                while True:
                    self.check()
                    chunk = await self.runtime._io(source.read(SFTP_CHUNK_BYTES))
                    if not chunk:
                        break
                    transferred += len(chunk)
                    if transferred > node.size:
                        raise TreePlanError("A remote source file grew during transfer")
                    stream.write(chunk)
                    self.progress(transferred)
                after = await self.runtime._io(source.stat())
                if transferred != node.size or remote_version(after) != remote_version(node.source):
                    raise TreePlanError("A remote source file changed during transfer")
            self.check()

    async def run(self):
        self.progress(planning=True)
        async with asyncio.timeout(TREE_PLAN_TIMEOUT):
            if self.upload:
                await self._guard_remote_directory(posixpath.dirname(self.request.remote_path))
                await self._scan_local()
            else:
                with local_directory(self.local.parent):
                    pass
                await self._scan_remote()
            await self._destinations()
            await self._validate_source_directories()
        self.check()
        self.progress()
        await self._directories()
        for node in self.nodes:
            if node.directory or (
                node.destination is not None and self.request.tree_policy is SftpTreePolicy.SKIP
            ):
                continue
            self.check()
            if self.upload:
                await self._upload_file(node)
            else:
                await self._download_file(node)
            self.summary = replace(
                self.summary,
                files_completed=self.summary.files_completed + 1,
                transferred=self.summary.transferred + node.size,
            )
            self.progress()
        await self._validate_source_directories()
        return SftpResult(self.request.request_id, "ok", "Folder completed")

    def result(self, result):
        summary = self.summary
        message = (
            f"{result.message} · {summary.files_completed}/{summary.files_total} files confirmed"
            f" · {summary.directories_created} folder creations confirmed"
            f" · {summary.skipped_existing} existing files skipped"
            f" · {summary.skipped_links} links/special entries excluded"
        )
        if result.status not in ("ok", "overwrite-required"):
            message += "; completed files/folders are kept, not rolled back"
        return replace(result, message=message, tree_summary=summary)
