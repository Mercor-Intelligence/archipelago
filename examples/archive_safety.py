import shutil
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath


def safe_archive_member_path(member_name: str) -> PurePosixPath:
    """Validate an archive member name and return a safe relative POSIX path."""
    clean_name = member_name[:-1] if member_name.endswith("/") else member_name
    if not clean_name:
        raise ValueError("Archive member path cannot be empty")
    if "\x00" in clean_name:
        raise ValueError(f"Archive member path contains a null byte: {member_name!r}")
    if "\\" in clean_name:
        raise ValueError(
            f"Archive member path must use '/' separators only: {member_name!r}"
        )

    posix_path = PurePosixPath(clean_name)
    windows_path = PureWindowsPath(clean_name)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"Archive member path must be relative: {member_name!r}")

    parts = clean_name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Archive member path is not normalized: {member_name!r}")

    return posix_path


def safe_extract_zip(zip_file: zipfile.ZipFile, target_dir: Path) -> None:
    """Extract a zip file without allowing members to escape target_dir."""
    target_root = target_dir.resolve()
    for member in zip_file.infolist():
        relative_path = safe_archive_member_path(member.filename)
        destination = target_root.joinpath(*relative_path.parts)
        try:
            destination.resolve().relative_to(target_root)
        except ValueError as e:
            raise ValueError(
                f"Archive member would extract outside target directory: {member.filename!r}"
            ) from e

        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        with zip_file.open(member) as source, open(destination, "wb") as target:
            shutil.copyfileobj(source, target)
