"""Explicit diagnostic with opt-in checkpoints; never install or run at startup."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from termana.build_info import application_version
from termana.updates.client import UpdateCheckpoint
from termana.updates.discovery import discover_update as check_for_update
from termana.updates.manifest import ReleaseRepository, UpdateError
from termana.updates.official import PUBLIC_KEY, REPOSITORY, native_target
from termana.updates.state import UpdateStateStore


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m termana.updates",
        description="Check signed update metadata; never install. Only --state-directory writes updater checkpoints.",
    )
    parser.add_argument("--repository", default=REPOSITORY.identity, metavar="OWNER/REPO")
    parser.add_argument(
        "--public-key-file",
        type=Path,
        help="Trusted local file containing exactly 32 raw Ed25519 public-key bytes",
    )
    parser.add_argument("--channel", choices=("stable", "qa"), default="stable")
    parser.add_argument("--current-version", default=application_version())
    parser.add_argument("--system", choices=("macos", "windows", "linux"))
    parser.add_argument("--architecture", choices=("x86_64", "arm64"))
    parser.add_argument("--accepted-sequence", type=int)
    parser.add_argument("--accepted-digest")
    parser.add_argument(
        "--state-directory",
        type=Path,
        help="Explicit private updater directory; persist accepted checkpoints here",
    )
    args = parser.parse_args(arguments)
    if args.state_directory is not None and (
        args.accepted_sequence is not None or args.accepted_digest is not None
    ):
        parser.error("Do not combine persistent state with manual checkpoint arguments")
    if (args.accepted_sequence is None) != (args.accepted_digest is None):
        parser.error("Supply both accepted sequence and digest, or neither for a first check")
    try:
        repository = ReleaseRepository.parse(args.repository)
        if args.public_key_file is None:
            if repository != REPOSITORY:
                raise UpdateError("A custom repository requires its explicit trusted public key")
            key = PUBLIC_KEY
        else:
            with args.public_key_file.open("rb") as stream:
                key = stream.read(33)
        system, architecture = native_target()
        checkpoint = (
            None
            if args.accepted_sequence is None
            else UpdateCheckpoint(
                repository,
                args.channel,
                args.accepted_sequence,
                args.accepted_digest,
            )
        )
        state = None if args.state_directory is None else UpdateStateStore(args.state_directory)
        if state is not None:
            checkpoint = state.load(repository, args.channel, key)
        result = check_for_update(
            repository,
            key,
            channel=args.channel,
            current_version=args.current_version,
            system=args.system or system,
            architecture=args.architecture or architecture,
            now=datetime.now(UTC),
            checkpoint=checkpoint,
        )
        if state is not None:
            state.accept(result.checkpoint, key)
    except (OSError, UpdateError) as error:
        message = str(error) if isinstance(error, UpdateError) else "Cannot read pinned public key"
        print(json.dumps({"status": "error", "message": message}))
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "version": result.manifest.version,
                "channel": result.manifest.channel,
                "accepted_sequence": result.checkpoint.sequence,
                "accepted_digest": result.checkpoint.sha256,
                "asset": None if result.artifact is None else result.artifact.name,
                "installed": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
