"""Localize owned SFTP feedback without changing transport results or user data."""

from termana.i18n import current_language, tr
from termana.models.settings import InterfaceLanguage
from termana.models.sftp import SftpOperation, SftpRequest, SftpResult


def sftp_result_text(request: SftpRequest, result: SftpResult) -> str:
    """Leave unknown diagnostics literal; rebuild only a recognized owned envelope."""
    if current_language() is InterfaceLanguage.ENGLISH:
        return result.message
    text = result.message
    summary = result.tree_summary
    if summary is not None:
        suffix = (
            f" · {summary.files_completed}/{summary.files_total} files confirmed"
            f" · {summary.directories_created} folder creations confirmed"
            f" · {summary.skipped_existing} existing files skipped"
            f" · {summary.skipped_links} links/special entries excluded"
        )
        if result.status not in ("ok", "overwrite-required"):
            suffix += "; completed files/folders are kept, not rolled back"
        if not text.endswith(suffix):
            return text
        text = text[: -len(suffix)]
    prefix = request.operation.label + ": "
    retry_notice = "; check destination state before retrying"
    if text.startswith(prefix):
        reason = text[len(prefix) :]
        retry = request.operation is not SftpOperation.LIST and reason.endswith(retry_notice)
        if retry:
            reason = reason[: -len(retry_notice)]
        text = tr(
            "{action}: {reason}",
            action=tr(request.operation.label),
            reason=tr(reason, context="sftp-reason"),
        )
        if retry:
            text += tr("; check destination state before retrying")
    else:
        text = tr(text, context="sftp-status")
    if summary is not None:
        text += tr(
            " · {completed}/{total} files confirmed · {folders} folder creations confirmed · {skipped} existing files skipped · {links} links/special entries excluded",
            completed=summary.files_completed,
            total=summary.files_total,
            folders=summary.directories_created,
            skipped=summary.skipped_existing,
            links=summary.skipped_links,
        )
        if result.status not in ("ok", "overwrite-required"):
            text += tr("; completed files/folders are kept, not rolled back")
    return text
