"""Recovery of owner-only state permissions after external mode resets.

Restoring a persistent home volume can re-apply group access onto the
directories and files portmap created owner-only (directories ``0700`` ->
``2770``, records ``0600`` -> ``0660``). The state gates fail closed on any
such relaxation; this module lets a gate tighten an owned path back to its
required mode, but only while that is provably harmless:

* a descriptor walk opens every component from the filesystem root down to
  the path with ``O_NOFOLLOW``, so no symlink can participate and a swap
  cannot redirect the chmod - it always applies to the verified descriptor;
* the closest ancestor directory that is owned by this user and denies
  group and other access (``mode & 0o077 == 0``) proves nobody else could
  reach, replace or modify anything below it, so group bits added below it
  are inert and removing them cannot affect anyone else;
* the proof is computed from the modes observed *before* any chmod, and a
  directory never authorizes its own recovery - only the descriptors the
  walk verified are tightened. Privacy restored this way always traces
  back to an ancestor that was private before portmap ran, so file
  recovery below a recovered directory inherits the same original proof.

Anything else keeps failing closed: paths without a private owned ancestor
(for example under the world-traversable ``/tmp``), foreign-owned hops,
symlinks, and owner bits a reset stripped (recovery only ever removes
group/other bits; it never re-grants owner permissions). Agent runtime
directories retain their metadata-only compatibility rule for readable,
non-writable paths; writable state requires the same private-ancestor proof.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .errors import PortmapError

_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _uid() -> int:
    return os.getuid()


def _descriptor_chain(path: Path) -> list[int]:
    """Descriptors for every component from the filesystem root to ``path``.

    Each hop is opened relative to its parent descriptor with ``O_NOFOLLOW``,
    so a symlink anywhere in the chain fails with ELOOP instead of silently
    redirecting the walk, and unreadable or missing hops raise ``OSError``.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise OSError(errno.EINVAL, "permission recovery requires a canonical absolute path")
    descriptors = [os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_CLOEXEC)]
    try:
        for component in path.parts[1:]:
            descriptors.append(os.open(component, _OPEN_FLAGS, dir_fd=descriptors[-1]))
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    return descriptors


def _protected(descriptors: list[int], *, target_included: bool) -> bool:
    """Whether the walk proves the chain owned and shielded below one root.

    Every hop must be a directory. The closest hop owned by this user with
    no group or other access bounds the protected scope; every hop below it
    (including the recovery target when ``target_included``) must belong to
    this user as well.
    """
    infos = [os.fstat(descriptor) for descriptor in descriptors]
    if any(not stat.S_ISDIR(info.st_mode) for info in infos):
        return False
    candidates = infos[:-1] if target_included else infos
    scope = None
    for index, info in enumerate(candidates):
        if info.st_uid == _uid() and stat.S_IMODE(info.st_mode) & 0o077 == 0:
            scope = index
    if scope is None:
        return False
    return all(info.st_uid == _uid() for info in infos[scope + 1:])


def _tighten_descriptor(path: Path, fd: int, target: int, *, message: str) -> None:
    """Prove the parent chain of an open descriptor, then fchmod it."""
    parent = Path(path).parent
    if not parent.is_absolute():
        raise PortmapError(message)
    try:
        descriptors = _descriptor_chain(parent)
    except OSError as exc:
        raise PortmapError(message) from exc
    try:
        if not _protected(descriptors, target_included=False):
            raise PortmapError(message)
        linked = os.stat(path.name, dir_fd=descriptors[-1], follow_symlinks=False)
        opened = os.fstat(fd)
        if (not stat.S_ISREG(linked.st_mode) or opened.st_nlink != 1
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)):
            raise PortmapError(message)
        try:
            os.fchmod(fd, target)
        except OSError as exc:
            raise PortmapError(message) from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def ensure_private_directory(path: Path, *, message: str, create: bool = True, exact: bool = True) -> None:
    """Private-state gate for a directory, with safe volume-reset recovery.

    With ``create`` the named directory is created ``0700`` with the same
    parent-creation behavior as the previous gates. A directory that already
    satisfies the gate passes untouched: ``exact`` gates demand an owned
    directory with mode ``0700`` (an inherited setgid bit is tolerated, as
    before), non-exact gates only the absence of group/other access. Any
    other owned directory whose whole chain is provably protected by a
    private owned ancestor is tightened back on the verified descriptor -
    setgid preserved where the kernel allows it - and everything else fails
    closed with ``message``. Only the named directory is touched; nothing
    is recursed into and no other metadata changes.
    """
    path = Path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != _uid():
        raise PortmapError(message)
    if exact:
        compliant = metadata.st_mode & 0o777 == 0o700
    else:
        compliant = not metadata.st_mode & 0o077
    if compliant:
        return
    if not path.is_absolute():
        raise PortmapError(message)
    try:
        descriptors = _descriptor_chain(path)
    except OSError as exc:
        raise PortmapError(message) from exc
    try:
        info = os.fstat(descriptors[-1])
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != _uid():
            raise PortmapError(message)
        if exact and mode & 0o700 != 0o700:
            # A reset that also stripped owner access is not a plain group
            # relaxation; refuse instead of re-granting owner permissions.
            raise PortmapError(message)
        if not _protected(descriptors, target_included=True):
            raise PortmapError(message)
        target = (mode & 0o2000) | 0o700 if exact else mode & ~0o077
        try:
            os.fchmod(descriptors[-1], target)
        except OSError as exc:
            if not (exact and mode & 0o2000 and exc.errno == errno.EPERM):
                raise PortmapError(message) from exc
            # The setgid group cannot be re-applied by this user; owner-only
            # access is what the gates require.
            try:
                os.fchmod(descriptors[-1], 0o700)
            except OSError as retry:
                raise PortmapError(message) from retry
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def ensure_private_file(path: Path, *, message: str, strip: int = 0o077, force: int | None = None) -> None:
    """Private-state gate for a file, with the same safe recovery.

    An absent file is left to the caller's not-found handling. An owned
    regular file already satisfying its gate passes untouched: by default
    no bit of ``strip`` (group/other access) may be set, or the mode must
    equal ``force`` exactly. Otherwise the parent chain is proved exactly
    like for directories and only the offending bits are removed (or the
    mode is set to ``force``, never re-granting owner write access).
    Foreign, unexpected or unprovable files fail closed with ``message``;
    content is never modified or removed.
    """
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != _uid():
        raise PortmapError(message)
    mode = stat.S_IMODE(metadata.st_mode)
    if (force is not None and mode == force) or (force is None and not mode & strip):
        return
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PortmapError(message) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != _uid():
            raise PortmapError(message)
        mode = stat.S_IMODE(info.st_mode)
        if force is not None:
            if mode == force:
                return
            if mode & 0o600 != 0o600:
                # Re-granting owner write access is not tightening.
                raise PortmapError(message)
            _tighten_descriptor(path, fd, force, message=message)
        elif mode & strip:
            _tighten_descriptor(path, fd, mode & ~strip, message=message)
    finally:
        os.close(fd)


def tighten_private_descriptor(path: Path, fd: int, *, message: str, strip: int = 0o077) -> None:
    """Recovery for a state file the caller already opened with O_NOFOLLOW.

    No-op when no bit of ``strip`` is set. Otherwise the descriptor must
    reference an owned regular file; the parent chain of ``path`` is proved
    like for directories and only the ``strip`` bits are removed from the
    verified descriptor, which stays owned by the caller.
    """
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if not mode & strip:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != _uid():
        raise PortmapError(message)
    _tighten_descriptor(path, fd, mode & ~strip, message=message)
