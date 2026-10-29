"""Stage runtime package metadata without editable-install paths or private README."""

from pathlib import Path

if __package__:
    from .public_source import audit_payload
else:
    from public_source import audit_payload


def stage_metadata(source, destination, *, project_readme=None):
    source, destination = Path(source), Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Linked package metadata refused")
        if not path.is_file() or path.name in {"direct_url.json", "RECORD", "REQUESTED"}:
            continue
        name = path.relative_to(source)
        # Runtime discovery and license compliance need these fields, not build
        # SBOMs, installer receipts or source-runner path inventories.
        if not (
            name.parts[0] == "licenses"
            or path.name.upper().startswith(("LICENSE", "COPYING"))
            or (
                len(name.parts) == 1
                and path.name in {"METADATA", "WHEEL", "entry_points.txt", "top_level.txt"}
            )
        ):
            continue
        data = path.read_bytes()
        if project_readme is not None and name.as_posix() == "METADATA":
            header, separator, _body = data.partition(b"\n\n")
            if not separator:
                raise ValueError("Malformed project package metadata")
            data = header + separator + project_readme
        audit_payload(name.as_posix(), data)
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(data)
    return destination
