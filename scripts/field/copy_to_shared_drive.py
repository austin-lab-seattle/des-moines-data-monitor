"""Copy stable snapshots of local instrument data into the OneDrive share.

The local acquisition tree remains authoritative. This job never deletes from
either side and never writes into the source. Each destination file is replaced
atomically only after the source remained unchanged for the complete read.
Locked or actively changing files are deferred to the next scheduled run.
"""

import argparse
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path


LOGGER = logging.getLogger("shared-drive-copy")
COPY_BUFFER_SIZE = 1024 * 1024
RETRY_DELAYS = (0.25, 1.0, 2.0)


def configure_logging(log_file):
    handlers = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def validate_roots(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_dir():
        raise ValueError(f"local data directory does not exist: {source}")
    source_key = os.path.normcase(str(source))
    destination_key = os.path.normcase(str(destination))
    try:
        common = os.path.commonpath((source_key, destination_key))
    except ValueError:
        common = ""
    if common == source_key:
        raise ValueError("shared-drive destination cannot be inside the source tree")
    return source, destination


def file_signature(stat_result):
    return stat_result.st_size, stat_result.st_mtime_ns


def destination_is_current(source_stat, destination):
    try:
        return file_signature(destination.stat()) == file_signature(source_stat)
    except FileNotFoundError:
        return False


def copy_stable_snapshot(source, destination):
    """Copy one file, returning copied, unchanged, deferred, or error."""
    last_error = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        temporary = None
        try:
            before = source.stat()
            if destination_is_current(before, destination):
                return "unchanged", None

            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".partial",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as out:
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, out, length=COPY_BUFFER_SIZE)
                    out.flush()
                    os.fsync(out.fileno())
                    after = os.fstat(source_handle.fileno())

            if file_signature(before) != file_signature(after):
                temporary.unlink(missing_ok=True)
                return "deferred", "source changed while it was being copied"

            os.utime(temporary, ns=(after.st_atime_ns, after.st_mtime_ns))
            os.replace(temporary, destination)
            return "copied", None
        except FileNotFoundError as exc:
            if temporary:
                temporary.unlink(missing_ok=True)
            return "deferred", f"source moved or rotated during copy: {exc}"
        except OSError as exc:
            last_error = exc
            if temporary:
                temporary.unlink(missing_ok=True)
            if attempt < len(RETRY_DELAYS):
                time.sleep(delay)
    if isinstance(last_error, PermissionError) or getattr(last_error, "winerror", None) in {
        32,
        33,
    }:
        return "deferred", str(last_error)
    return "error", str(last_error)


def copy_tree(source, destination):
    copied = unchanged = deferred = errors = 0
    for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
        relative = source_file.relative_to(source)
        destination_file = destination / relative
        status, detail = copy_stable_snapshot(source_file, destination_file)
        if status == "copied":
            copied += 1
            LOGGER.info("Copied %s", relative)
        elif status == "unchanged":
            unchanged += 1
        elif status == "deferred":
            deferred += 1
            LOGGER.warning("Deferred %s: %s", relative, detail)
        else:
            errors += 1
            LOGGER.error("Failed %s: %s", relative, detail)
    LOGGER.info(
        "Shared-drive copy complete: copied=%d unchanged=%d deferred=%d errors=%d",
        copied,
        unchanged,
        deferred,
        errors,
    )
    return errors == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="canonical local data directory")
    parser.add_argument("--destination", required=True, help="OneDrive shared data directory")
    parser.add_argument("--log-file", help="optional local log file")
    parser.add_argument("--check", action="store_true", help="validate paths without copying")
    args = parser.parse_args()
    configure_logging(args.log_file)
    try:
        source, destination = validate_roots(args.source, args.destination)
        if args.check:
            destination.mkdir(parents=True, exist_ok=True)
            LOGGER.info("Copy paths are valid: %s -> %s", source, destination)
            return 0
        destination.mkdir(parents=True, exist_ok=True)
        return 0 if copy_tree(source, destination) else 1
    except (OSError, ValueError) as exc:
        LOGGER.error("Shared-drive copy could not start: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
