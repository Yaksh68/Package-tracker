"""Uploaded source archives — the scan input for repos with `source="upload"`.

Instead of cloning, an upload repo's scan unpacks the zip stored for that
version into a temp dir and hands the pipeline the same `Clone` a git clone
would, so extraction, image inspection, enrichment and flagging are unchanged.

The zip is kept (one per version, replaced by a re-upload) so scheduled
rescans keep working: the code is frozen, but upstream EOL data is not.

Archives are untrusted input. `save` rejects anything that isn't a zip, is too
big, or would unpack outside its directory; `unpack` re-applies the path checks,
skips symlinks and the dirs no scan reads (node_modules, .git, …), and caps the
unpacked size as it writes.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from .agents.clone import Clone, CloneError
from .agents.parsers import under_skipped
from .config import get_settings

_CHUNK = 1024 * 1024


class UploadError(ValueError):
    """The archive was rejected; the message is safe to show the user."""


def _dir(repo_id: str) -> Path:
    return Path(get_settings().upload_dir) / repo_id


def _file(repo_id: str, version: str) -> Path:
    # version labels may hold "/" or spaces; the hash keeps distinct labels apart
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", version).strip("._")[:60] or "v"
    digest = hashlib.sha1(version.encode()).hexdigest()[:8]
    return _dir(repo_id) / f"{slug}-{digest}.zip"


def has_upload(repo_id: str, version: str) -> bool:
    return _file(repo_id, version).is_file()


def stored_bytes(repo_id: str) -> int:
    """What the repo's kept zips take on disk."""
    d = _dir(repo_id)
    return sum(f.stat().st_size for f in d.glob("*.zip")) if d.is_dir() else 0


def version_bytes(repo_id: str, version: str) -> int:
    f = _file(repo_id, version)
    return f.stat().st_size if f.is_file() else 0


def _safe_parts(name: str) -> tuple[str, ...] | None:
    """Path parts of a zip entry, or None if it would escape the target dir."""
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
        return None
    return p.parts


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _kept(info: zipfile.ZipInfo) -> bool:
    parts = _safe_parts(info.filename)
    return bool(parts) and not info.is_dir() and not _is_symlink(info) and not under_skipped(parts)


def _check(zf: zipfile.ZipFile, max_unpacked_mb: int | None = None) -> None:
    s = get_settings()
    unpacked_mb = min(s.upload_max_unpacked_mb, max_unpacked_mb or s.upload_max_unpacked_mb)
    infos = zf.infolist()
    if any(_safe_parts(i.filename) is None for i in infos):
        raise UploadError("the zip contains paths that point outside the archive (../ or absolute)")
    kept = [i for i in infos if _kept(i)]
    if not kept:
        raise UploadError("the zip has no files to scan")
    if len(kept) > s.upload_max_files:
        raise UploadError(f"the zip has {len(kept)} files; the limit is {s.upload_max_files}")
    total = sum(i.file_size for i in kept)
    if total > unpacked_mb * _CHUNK:
        raise UploadError(f"the zip unpacks to {total // _CHUNK} MB; the limit is "
                          f"{unpacked_mb} MB (node_modules, .git etc. are not counted)")


def save(repo_id: str, version: str, src: BinaryIO, *, max_mb: int | None = None,
         max_unpacked_mb: int | None = None,
         before_replace: Callable[[int], None] | None = None) -> int:
    """Validate and store the archive for `version`, replacing any earlier one.
    Returns its size in bytes. `max_mb` / `max_unpacked_mb` lower the env's
    caps (the workspace's limits). `before_replace(size)` may raise to refuse
    a valid zip (e.g. the workspace's storage limit); the earlier zip is
    then left as it was."""
    s = get_settings()
    limit = min(s.upload_max_mb, max_mb or s.upload_max_mb) * _CHUNK
    _dir(repo_id).mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_dir(repo_id), suffix=".part")
    try:
        size = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := src.read(_CHUNK):
                size += len(chunk)
                if size > limit:
                    raise UploadError(f"the zip is larger than {limit // _CHUNK} MB")
                out.write(chunk)
        if not zipfile.is_zipfile(tmp):
            raise UploadError("the file is not a .zip archive")
        try:
            with zipfile.ZipFile(tmp) as zf:
                _check(zf, max_unpacked_mb)
        except zipfile.BadZipFile as e:
            raise UploadError(f"the zip is damaged: {e}") from e
        if before_replace is not None:
            before_replace(size)
        os.replace(tmp, _file(repo_id, version))
        return size
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def unpack(repo_id: str, version: str, *, max_unpacked_mb: int | None = None) -> Clone:
    """Unpack the stored archive into a temp dir (caller cleans up), returning
    it as a `Clone`. A zip of a single folder is rooted at that folder.
    `max_unpacked_mb` lowers the env's cap (the workspace's limit)."""
    path = _file(repo_id, version)
    if not path.is_file():
        raise CloneError(f"no zip uploaded for version '{version}' — upload one to scan it")
    tmp = tempfile.mkdtemp(prefix="packages-tracker-upload-")
    dest = Path(tmp) / "repo"
    s = get_settings()
    budget = min(s.upload_max_unpacked_mb, max_unpacked_mb or s.upload_max_unpacked_mb) * _CHUNK
    try:
        with zipfile.ZipFile(path) as zf:
            _check(zf, max_unpacked_mb)
            for info in zf.infolist():
                if not _kept(info):
                    continue
                target = dest.joinpath(*_safe_parts(info.filename))
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    # file_size in the header can lie — count what is written
                    while chunk := src.read(_CHUNK):
                        budget -= len(chunk)
                        if budget < 0:
                            raise UploadError("the zip unpacks to more than the size limit")
                        out.write(chunk)
    except (UploadError, zipfile.BadZipFile, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CloneError(f"could not unpack the uploaded zip: {e}") from e
    entries = list(dest.iterdir())
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else dest
    return Clone(path=root, _tmp=tmp)


def delete(repo_id: str, version: str | None = None) -> None:
    """Drop one version's archive, or (no version) every archive of the repo."""
    if version is None:
        shutil.rmtree(_dir(repo_id), ignore_errors=True)
    else:
        _file(repo_id, version).unlink(missing_ok=True)
