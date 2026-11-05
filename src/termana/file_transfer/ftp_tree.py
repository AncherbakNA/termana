"""Bounded FTP folder planning over the library client and guarded local I/O."""

import posixpath
import stat
import time
from contextlib import contextmanager
from dataclasses import replace

from termana.file_transfer.worker import JobCancelled, OverwriteRequired
from termana.models.sftp import (
    SftpOperation,
    SftpProgress,
    SftpRequest,
    SftpResult,
    SftpTreePolicy,
    SftpTreeSummary,
)
from termana.models.sftp_batch import local_download_name
from termana.sftp.sftp_tree import MAX_TREE_DEPTH, MAX_TREE_ENTRIES, MAX_TREE_NAME_BYTES
from termana.sftp.sftp_tree_local import is_link, local_directory
from termana.ssh.ssh_file_metadata import same_ssh_path_status


class FtpTreeTransfer:
    def __init__(self, backend, request):
        self.backend, self.request = backend, request
        self.nodes = []
        self.name_bytes = 0
        self.deadline = time.monotonic() + 120
        self.summary = SftpTreeSummary()
        self._source_files = {}
        self._source_directories = {}

    def _check(self):
        self.backend._check_cancel(self.request)
        if time.monotonic() > self.deadline:
            raise TimeoutError("FTP folder operation exceeded its planning/operation deadline")

    def _record(self, parts, kind, size=0):
        self._check()
        self.name_bytes += len("/".join(parts).encode("utf-8"))
        if (len(self.nodes) >= MAX_TREE_ENTRIES or len(parts) > MAX_TREE_DEPTH
                or self.name_bytes > MAX_TREE_NAME_BYTES):
            raise ValueError("FTP folder exceeds the supported bounds")
        self.nodes.append((parts, kind, size))

    def _scan_local(self):
        self._record((), "directory")
        stack = [()]
        while stack:
            parts = stack.pop()
            with local_directory(self.request.local_path.joinpath(*parts)) as directory:
                self._source_directories[parts] = directory.status()
                with directory.entries() as entries:
                    for entry in entries:
                        self._check()
                        local_download_name(entry.name)
                        status = directory.lstat(entry.name)
                        if status is None:
                            raise ValueError("Local folder changed during planning")
                        child = (*parts, entry.name)
                        if is_link(status):
                            self._record(child, "excluded")
                            self.summary = replace(self.summary, skipped_links=self.summary.skipped_links + 1)
                        elif stat.S_ISDIR(status.st_mode):
                            self._record(child, "directory")
                            stack.append(child)
                        elif stat.S_ISREG(status.st_mode):
                            self._record(child, "file", status.st_size)
                            self._source_files[child] = status
                        else:
                            self._record(child, "excluded")
                            self.summary = replace(self.summary, skipped_links=self.summary.skipped_links + 1)

    def _scan_remote(self):
        self._record((), "directory")
        stack = [()]
        while stack:
            parts = stack.pop()
            remote = posixpath.join(self.request.remote_path, *parts)
            for entry in self.backend._listing(remote, self.request):
                self._check()
                local_download_name(entry.name)
                child = (*parts, entry.name)
                if entry.kind == "directory":
                    self._record(child, "directory")
                    stack.append(child)
                elif entry.kind == "file":
                    self._record(child, "file", entry.size or 0)
                else:
                    self._record(child, "excluded")
                    self.summary = replace(self.summary, skipped_links=self.summary.skipped_links + 1)

    def _created(self, _path):
        self.summary = replace(self.summary, directories_created=self.summary.directories_created + 1)

    def _progress(self, event):
        self.backend._publish(SftpProgress(
            self.request.request_id, self.summary.transferred + event.transferred,
            self.summary.total, self.summary.files_completed, self.summary.files_total,
            directories_completed=self.summary.directories_created,
        ))

    def _result(self, status, message):
        summary = self.summary
        message += (
            f" · {summary.files_completed}/{summary.files_total} files confirmed"
            f" · {summary.directories_created} folder creations confirmed"
            f" · {summary.skipped_existing} existing files skipped"
            f" · {summary.skipped_links} links/special entries excluded"
        )
        if status not in ("ok", "overwrite-required"):
            message += "; completed files/folders are kept, not rolled back"
        return SftpResult(self.request.request_id, status, message, tree_summary=summary)

    def _upload(self):
        root = self.backend._stat(self.request.remote_path, self.request)
        if root is not None:
            if root.kind != "directory":
                raise ValueError("Remote folder target changed type")
            if self.request.tree_policy is SftpTreePolicy.ASK:
                raise OverwriteRequired
        for parts, kind, size in sorted(self.nodes, key=lambda n: (len(n[0]), n[0])):
            self._check()
            if kind == "excluded":
                continue
            remote = posixpath.join(self.request.remote_path, *parts)
            current = self.backend._stat(remote, self.request)
            if kind == "directory":
                if current is None:
                    self.backend._ftp.mkd(remote)
                    self._created(remote)
                elif current.kind != "directory":
                    raise ValueError("Remote directory target changed")
                continue
            if current is not None and self.request.tree_policy is SftpTreePolicy.SKIP:
                self.summary = replace(self.summary, skipped_existing=self.summary.skipped_existing + 1)
                continue
            if current is not None and self.request.tree_policy is not SftpTreePolicy.REPLACE:
                raise ValueError("A remote destination appeared during copying")
            child = SftpRequest(
                SftpOperation.UPLOAD, remote, self.request.local_path.joinpath(*parts),
                overwrite=current is not None, request_id=self.request.request_id,
            )
            @contextmanager
            def source(parts=parts, child=child):
                # Recheck every planned parent before opening the exact selected file.
                for length in range(len(parts)):
                    parent_parts = parts[:length]
                    with local_directory(self.request.local_path.joinpath(*parent_parts)) as parent:
                        if not same_ssh_path_status(self._source_directories[parent_parts], parent.status()):
                            raise ValueError("A local source directory changed after planning")
                with local_directory(child.local_path.parent) as parent:
                    with parent.source(child.local_path.name, self._source_files[parts]) as stream:
                        yield stream
            self.backend._upload(child, open_source=source)
            self.summary = replace(self.summary, files_completed=self.summary.files_completed + 1,
                                   transferred=self.summary.transferred + size)

    def _download(self):
        destination = self.request.local_path
        with local_directory(destination.parent) as parent:
            root = parent.lstat(destination.name)
            if root is not None:
                if is_link(root) or not stat.S_ISDIR(root.st_mode):
                    raise ValueError("Local folder target is not a regular directory")
                if self.request.tree_policy is SftpTreePolicy.ASK:
                    raise OverwriteRequired
        for parts, kind, size in sorted(self.nodes, key=lambda n: (len(n[0]), n[0])):
            self._check()
            if kind == "excluded":
                continue
            local = destination.joinpath(*parts)
            if kind == "directory":
                with local_directory(local, creation_root=destination.parent, created=self._created):
                    pass
                continue
            with local_directory(local.parent) as directory:
                current = directory.lstat(local.name)
                if current is not None and (is_link(current) or not stat.S_ISREG(current.st_mode)):
                    raise ValueError("Local file target changed type")
            if current is not None and self.request.tree_policy is SftpTreePolicy.SKIP:
                self.summary = replace(self.summary, skipped_existing=self.summary.skipped_existing + 1)
                continue
            if current is not None and self.request.tree_policy is not SftpTreePolicy.REPLACE:
                raise ValueError("A local destination appeared during copying")
            self.backend._download(SftpRequest(
                SftpOperation.DOWNLOAD, posixpath.join(self.request.remote_path, *parts), local,
                overwrite=current is not None, request_id=self.request.request_id,
            ))
            self.summary = replace(self.summary, files_completed=self.summary.files_completed + 1,
                                   transferred=self.summary.transferred + size)

    def run(self):
        try:
            upload = self.request.operation is SftpOperation.UPLOAD_TREE
            if upload:
                self._scan_local()
            else:
                root = self.backend._stat(self.request.remote_path, self.request)
                if root is None or root.kind != "directory":
                    raise ValueError("Remote folder is unavailable")
                self._scan_remote()
            self.summary = replace(self.summary,
                                   files_total=sum(kind == "file" for _, kind, _ in self.nodes),
                                   total=sum(size for _, kind, size in self.nodes if kind == "file"))
            # The scan is bounded separately; transfer time scales with file size.
            self.deadline = float("inf")
            self.backend._tree_progress = self._progress
            if upload:
                self._upload()
            else:
                self._download()
            self._progress(SftpProgress(self.request.request_id, 0, 0))
            return self._result("ok", "Folder completed")
        except OverwriteRequired:
            if not self.summary.files_completed and not self.summary.directories_created:
                return self._result("overwrite-required", "Destination folder exists")
            return self._result("error", "Folder changed during copying; inspect the destination")
        except Exception as error:
            cancelled = isinstance(error, JobCancelled) or self.backend._stop.is_set()
            return self._result(
                "cancelled" if cancelled else "error",
                "Folder transfer stopped. Check the destination before retrying.",
            )
        finally:
            self.backend._tree_progress = None
