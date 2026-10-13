"""受限丙（v1.5.5）：失败恢复时保留被认领的原对象，并让自检跨重启列全。

术语
----
*保留物*（retained object）：本次调用在撤销自己写入时，先原子认领（``os.replace``
到同级私有名）过的那个原文件对象。旧实现随后用 ``claim.unlink()`` 销毁它；本版
把**同一个 inode** 留在私有名字 ``.{原文件名}.{写者PID}.rollback`` 下，因此任何
在认领之前就已经打开文件描述符的写者，之后通过旧 fd 写进去的字节都还在这个对象
里。保留物不是备份：它是被保留的原对象本身。

发现机制（两层，互为校验）
--------------------------
1. 文件系统扫描（权威层）。保留物一定与它的原路径同目录（认领是同级 ``os.replace``），
   所以只需枚举固定发现范围内各目录的表层。即使进程在"认领完成、登记之前"崩溃，
   或者登记日志本身丢失，对象依然能被发现。扫描只看名字规则，不读正文。
2. 元数据登记（JSONL 追加日志，``<runtime_dir>/memory-retained/retained-objects.jsonl``）。
   每次保留追加一行，**只写元数据**：保留绝对路径、原绝对路径、任务、操作、原因、
   写者 PID、时间。不复制正文，也**不记录保留对象的正文哈希**——对象仍可由认领前
   打开的旧 fd 改写，用哈希"冻结"它既不真实，又会掩盖后来的写入。日志允许缺失或
   失配；扫描结果优先，两层不一致时按异常上报，绝不静默。

发现范围（``retained_scan_dirs``）：memory outbox、Vault 笔记目录、目录笔记所在目录、
KnowLP 图输出目录、runtime 目录本身。这些是保留物可能出现的全部位置。自检**只**
在这个范围内读对象：登记行指向范围外的路径时，它报告 ``record_outside_scope`` 而
不跟随，绝不把"核对"变成对任意目录的无界扫描。

处理记录（``retained-resolutions.jsonl``，本版新增）
----------------------------------------------------
保留物只能由**用户亲自**逐个处理，命令是 ``memory-retained-resolve``：要么把对象
移进系统废纸篓（``--trash``，只接受发现范围内、身份核验通过的那一个对象），要么
记录用户已在别处手动删除它（``--confirm-deleted``，只接受登记日志命名过的路径，
且对象必须真的不在了）。每次成功处理追加一行**只含元数据**的处理记录：对象标识
（dev/ino/size/mtime/mode）、原保留位置、处理方式、时间、实际废纸篓位置或"用户已
确认删除"、以及被这条记录解释掉的登记行摘要。自检据此把已处理对象从待办里去掉，
而**同一个路径后来出现的新对象**（不同 inode，或没有登记行）仍然是新的待办——
旧记录不会把它压掉。处理记录不复制正文、不采用为经验、不进任何检索。

移动与误捕获（``retained-move-intents.jsonl``）
-----------------------------------------------
把保留物移进系统废纸篓只用一次**排他**目录项移动（``renamex_np`` 配
``RENAME_EXCL``）：目标名已被占用时内核以 ``EEXIST`` 拒绝，而不是覆盖，所以
"先查名、再移动"之间的覆盖窗口不存在，移动链上也没有 staging 名、没有按路径
``unlink``。移动完成后按物理身份核对实际捕获到的对象：不是记录点名的那个，就用
第二次排他移动原样放回；放不回去（记录中的路径又被占用）时它留在废纸篓里的真实
位置，并向移动公告记录追加一行 ``captured_object``。自检跨重启按身份列出每个被
捕获对象的实际位置与原因，``is_retained_path`` 依据同一行把它排除出检索——绝不按
名字认领、绝不删除、绝不采用为内容。平台没有排他移动原语时命令拒绝移动，不用普通
``os.rename`` 顶替（那会覆盖目标上后来出现的未知对象）。

异常处理：目录列不出来、对象 stat/打开失败、日志行损坏或日志不可写，都转成显式
异常/问题条目，不抛给调用者。保留对象这件事本身**永不**因为"报告写不出来"而失败。

隔离：``is_retained_path`` 是检索/索引入口用的判据，两道门各自独立——(a) 名字规则，
(b) 登记/扫描得到的保留绝对路径集合。只看后缀不够：被人工改过名字的保留物靠
(b) 兜住，指向保留路径的索引条目也靠 (b) 兜住。已处理对象的路径不再被旧记录排除，
所以同一个路径上后来出现的正常文件可以重新被检索；未被撤销的 ``captured_object``
记录所点名的位置同样被排除，而那是一条废纸篓里的名字。
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

try:  # POSIX advisory file locks; absent on a few platforms, where the run says so.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

JOURNAL_SCHEMA = 1
RESOLUTION_SCHEMA = 1
INTENT_SCHEMA = 1
JOURNAL_DIRNAME = "memory-retained"
JOURNAL_FILENAME = "retained-objects.jsonl"
RESOLUTIONS_FILENAME = "retained-resolutions.jsonl"
MOVE_INTENTS_FILENAME = "retained-move-intents.jsonl"
REPORT_FILENAME = "retained-report.json"
LOCK_FILENAME = "retained-resolutions.lock"
# Trash-name prefix for the one case a record cannot name an object: a move captured
# something by mistake and the record that would list it could not be written.  The rest
# of the name carries the announced inode (already on disk before the move) and the inode
# of the object that was really captured, so a later run rebuilds the exact location and
# checks the object it finds against the name itself: an object counts only when it really
# carries the inode the name claims.
CAPTURED_KEPT_PREFIX = ".captured-object."
RETAINED_SUFFIXES = ("rollback", "restore")
MAX_SCAN_ENTRIES = 4096
MAX_REPORT_TEMP_ATTEMPTS = 64
MAX_TRASH_NAME_ATTEMPTS = 999
# A run waits for the cross-process lock this long before reporting that another
# process is still handling an object; it is short, bounded and never a busy loop.
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.01
ACTIONS = ("trash", "confirm_deleted")
# Object states that count as "something is still here and the user has to look".
# ``record_without_object`` is a record about an object that is gone, and the
# out-of-scope state is a record this check deliberately refuses to follow.
RETAINED_OBJECT_STATES = ("retained_object", "stale_staging_name", "unverifiable_object")

# ``O_NOFOLLOW`` keeps every write on a name that is not a symlink.  It is absent
# on a few platforms, where the explicit ``lstat`` checks below still apply.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# ``renamex_np`` with ``RENAME_EXCL`` is the one directory-entry move that can never
# replace anything: a target name that is already taken fails with ``EEXIST`` instead
# of being overwritten, so "check the name, then move" needs no separate check at all.
# A plain ``os.rename`` is never a substitute, because between a check and the move
# another writer can put an unknown object on the target name.
_RENAME_EXCL = 0x4


def _exclusive_move_function() -> Optional[Any]:
    """Return the platform's exclusive directory-entry move, or ``None`` without one."""
    try:
        function = ctypes.CDLL(None, use_errno=True).renamex_np
    except (OSError, AttributeError):  # pragma: no cover - no such primitive here
        return None
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


_EXCLUSIVE_MOVE = _exclusive_move_function()

_RETAINED_NAME = re.compile(r"^\.(?P<original>.+?)\.(?P<pid>\d+)\.(?P<suffix>rollback|restore)$")


def parse_retained_name(name: str) -> Optional[Dict[str, Any]]:
    """Return ``{"original", "pid", "suffix"}`` for a private recovery name."""
    matched = _RETAINED_NAME.match(name)
    if matched is None:
        return None
    return {
        "original": matched.group("original"),
        "pid": int(matched.group("pid")),
        "suffix": matched.group("suffix"),
    }


def is_retained_name(name: str) -> bool:
    return _RETAINED_NAME.match(name) is not None


def _key(path: Any) -> str:
    # ``realpath``, not ``abspath``: macOS exposes /var as a symlink to /private/var,
    # and config.runtime_dir is resolved while vault/graph paths are not.  abspath
    # would keep both spellings, so a journal record and the filesystem scan would
    # describe the same object under two different keys and every record would look
    # missing (or every object would).  realpath canonicalises both sides.
    return os.path.realpath(str(path))


def _record_key(path: Any) -> str:
    """Canonicalise a path *without* following a symlink standing at its last name.

    ``_key`` uses ``realpath``, which also resolves the final component.  A record that
    names a path which has since been replaced by a symlink would then be looked up --
    and reported -- under the path that symlink points at, i.e. this module would have
    followed an unknown link and listed whatever it found there.  Only the directory is
    canonicalised here, so the /var and /private/var spellings of one object still share
    a single key while the object's own name stays exactly what the record says.
    """
    raw = os.path.abspath(str(path))
    parent, name = os.path.split(raw)
    if not name:
        return os.path.realpath(raw)
    return os.path.join(os.path.realpath(parent), name)


def journal_path(config: Any) -> Path:
    runtime = Path(str(getattr(config, "runtime_dir", "") or ".")).expanduser()
    return runtime / JOURNAL_DIRNAME / JOURNAL_FILENAME


def resolutions_path(config: Any) -> Path:
    runtime = Path(str(getattr(config, "runtime_dir", "") or ".")).expanduser()
    return runtime / JOURNAL_DIRNAME / RESOLUTIONS_FILENAME


def report_path(config: Any) -> Path:
    runtime = Path(str(getattr(config, "runtime_dir", "") or ".")).expanduser()
    return runtime / JOURNAL_DIRNAME / REPORT_FILENAME


def move_intents_path(config: Any) -> Path:
    """The move-intent record: written before a move, never a second source of truth."""
    runtime = Path(str(getattr(config, "runtime_dir", "") or ".")).expanduser()
    return runtime / JOURNAL_DIRNAME / MOVE_INTENTS_FILENAME


@contextmanager
def _resolution_lock(config: Any) -> Iterator[Dict[str, Any]]:
    """Serialize "is this object already handled?" together with the record that answers it.

    Yields ``{"status": ..., "detail": ..., "lock_path": ...}``:

    * ``locked`` -- this process holds an exclusive ``flock`` on a lock file beside the
      resolution record, for the whole read-decide-act-record section;
    * ``busy`` -- another process still holds it after the bounded wait; the caller then
      changes nothing and reports exactly that;
    * ``unavailable`` -- no lock could be taken (no ``flock`` on this platform, or the
      lock file cannot be created or opened); the caller changes nothing either, because
      a single-object change made without its serialization lock is exactly the double
      handling this lock exists to prevent.

    The metadata directory is created first when it is missing, so the very first run in
    a fresh runtime still takes its lock instead of degrading to ``unavailable``.

    The kernel releases the lock when the process exits, so a writer that dies never
    blocks a later run, and no lock state has to be cleaned up by hand.
    """
    path = resolutions_path(config).with_name(LOCK_FILENAME)
    state: Dict[str, Any] = {"status": "unavailable", "detail": "", "lock_path": str(path)}
    if fcntl is None:
        state["detail"] = "this platform has no flock"
        yield state
        return
    directory_error = _ensure_metadata_directory(path.parent)
    if directory_error is not None:
        state["detail"] = f"the lock file {path} cannot be created: {directory_error}"
        yield state
        return
    try:
        handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        state["detail"] = f"the lock file {path} cannot be created: {exc}"
        yield state
        return
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state["status"] = "locked"
                break
            except OSError:
                if time.monotonic() >= deadline:
                    state["status"] = "busy"
                    break
                time.sleep(LOCK_POLL_SECONDS)
        yield state
    finally:
        try:
            os.close(handle)
        except OSError:  # pragma: no cover - the close never decides an outcome
            pass


def trash_dir(config: Any) -> Path:
    """The user's system trash, or the configured stand-in used by tests."""
    memory = getattr(config, "memory", None)
    memory = memory if isinstance(memory, dict) else {}
    configured = memory.get("retained_trash_dir")
    if configured:
        return Path(str(configured)).expanduser()
    return Path.home() / ".Trash"


def retained_scan_dirs(config: Any) -> List[Path]:
    """Directories that can hold a retained object, deduplicated and ordered."""
    memory = getattr(config, "memory", None)
    memory = memory if isinstance(memory, dict) else {}
    candidates: List[Path] = []
    runtime = getattr(config, "runtime_dir", None)
    if runtime:
        candidates.append(Path(str(runtime)).expanduser())
        candidates.append(Path(str(runtime)).expanduser() / "memory-outbox")
    vault = memory.get("obsidian_vault")
    if vault:
        root = Path(str(vault)).expanduser()
        subdir = memory.get("obsidian_subdir") or "AgentOS"
        candidates.append(root / str(subdir))
        catalog = memory.get("catalog_note")
        if catalog:
            candidates.append((root / str(catalog)).parent)
    graph = memory.get("knowlp_graph_dir")
    if graph:
        candidates.append(Path(str(graph)).expanduser())
    ordered: List[Path] = []
    seen: Set[str] = set()
    for candidate in candidates:
        key = _key(candidate)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(Path(key))
    return ordered


def _within_scope(key: str, scope_dirs: Any) -> bool:
    """Is ``key`` (an already-realpath'd absolute path) inside the discovery scope?"""
    for directory in scope_dirs or ():
        base = str(directory).rstrip(os.sep) or os.sep
        if key == base or key.startswith(base + os.sep):
            return True
    return False


def _ensure_metadata_directory(directory: Path) -> Optional[str]:
    """Create the private metadata directory, or explain why it cannot be used.

    The directory must be a real directory, not a symlink: metadata written
    through an unknown symlink would land somewhere else entirely.
    """
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return f"{directory}: {exc}"
    try:
        info = directory.lstat()
    except OSError as exc:
        return f"{directory}: {exc}"
    if stat.S_ISLNK(info.st_mode):
        return f"{directory} is a symlink; refusing to write metadata through it"
    if not stat.S_ISDIR(info.st_mode):
        return f"{directory} is not a directory"
    # Tighten a directory that is looser than private, but never loosen one: a
    # directory the user deliberately made read-only must stay read-only (that is
    # exactly how an unwritable metadata location is expressed, and this code must
    # report the failure instead of silently re-opening the directory).
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return None


def _append_metadata_line(target: Path, entry: Dict[str, Any]) -> Optional[str]:
    """Append one metadata line, never through a symlink.  ``None`` means written."""
    directory_error = _ensure_metadata_directory(target.parent)
    if directory_error is not None:
        return directory_error
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        return f"{target}: {exc}"
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        return f"{target} is a symlink; refusing to append a record through it"
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            str(target), os.O_CREAT | os.O_APPEND | os.O_WRONLY | _NOFOLLOW, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        os.write(
            descriptor,
            (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.fsync(descriptor)
    except OSError as exc:
        return f"{target}: {exc}"
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return None


def _row_digest(row: Dict[str, Any]) -> str:
    """A stable digest of one journal line, used to say "this record is explained".

    Both the resolution record and the reader compute it from the *parsed* line,
    so a line that is reformatted on disk still digests the same.
    """
    return hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def record_retained(
    config: Any,
    *,
    retained_path: Path,
    original_path: Path,
    operation: str,
    reason: str,
    task_id: Optional[str] = None,
    written_bytes_sha256: Optional[str] = None,
    writer_pid: Optional[int] = None,
    object_identity: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Append one metadata-only discovery record.

    The record also carries the *physical identity* (device and inode) of the
    object it names, because a path is not an identity: after the claim the very
    same path can be written by a later process, and a record that only names a
    path cannot tell the recorded object from a replacement.  The identity is
    observed with ``lstat`` at the moment the record is written unless the caller
    already has it; when it cannot be observed the record says so and carries no
    identity, and the move command then refuses to treat whatever sits at that
    path as the recorded object.

    Returns ``None`` on success, or the error text so the caller can report the
    real retained location together with the fact that the record is missing.
    Never raises: retention must not fail because the record could not be written.
    """
    if config is None:
        return "no configuration is available to record the retained object"
    target = journal_path(config)
    parsed = parse_retained_name(retained_path.name) or {}
    identity, identity_source, identity_note = _observed_identity(
        retained_path, object_identity
    )
    entry = {
        "schema": JOURNAL_SCHEMA,
        "kind": "retained_object",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retained_path": _record_key(retained_path),
        "original_path": _key(original_path),
        "suffix": parsed.get("suffix", "unknown"),
        "writer_pid": int(writer_pid if writer_pid is not None else parsed.get("pid", os.getpid())),
        "task_id": task_id or "unknown",
        "operation": operation or "unknown",
        "reason": reason or "unknown",
        "written_bytes_sha256": written_bytes_sha256 or "not_recorded",
        # The identity of the object this record names, so the move command can
        # verify that the object at the path is still the recorded one.
        "object_identity": identity,
        "identity_source": identity_source,
        "identity_note": identity_note,
        # The retained object is the original file object, not a copy of it, and it
        # stays writable through file descriptors opened before the claim.  Its
        # content is therefore deliberately not frozen here.
        "content_frozen": False,
        "content_copied": False,
        "content_note": (
            "metadata only: the retained object may still be written through file "
            "descriptors opened before the claim, so no content hash is recorded"
        ),
    }
    return _append_metadata_line(target, entry)


def _observed_identity(
    retained_path: Path, supplied: Optional[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """The physical identity to record, plus where it came from and what it means.

    Returns ``(identity, source, note)``.  ``identity`` is ``None`` only when no
    identity could be observed, which is itself recorded: a record without an
    identity must never be treated as if it verified anything.
    """
    if isinstance(supplied, dict) and supplied.get("ino") is not None:
        return (
            {
                "dev": int(supplied["dev"]),
                "ino": int(supplied["ino"]),
                "size_bytes": supplied.get("size_bytes"),
                "mtime_ns": supplied.get("mtime_ns"),
                "mode": supplied.get("mode"),
            },
            "supplied_by_caller",
            (
                "the identity was supplied by the caller that captured the object, so it "
                "names the object that was moved aside, not whatever a later lstat of the "
                "path would find"
            ),
        )
    try:
        observed = os.lstat(str(retained_path))
    except OSError as exc:
        return (
            None,
            "unavailable",
            (
                f"the object identity could not be observed when this record was written "
                f"({exc}); this record carries no object identity, so the move command will "
                "not treat whatever sits at this path as the recorded object"
            ),
        )
    if not stat.S_ISREG(observed.st_mode):
        return (
            None,
            "unavailable",
            (
                f"{retained_path} did not hold a regular file when this record was written; "
                "this record carries no object identity"
            ),
        )
    return (
        _identity_of(observed),
        "observed_when_recorded",
        (
            "the identity was observed with lstat on this path at the moment the record was "
            "written; it is the object that was moved aside, not a copy and not a hash"
        ),
    )


def record_resolution(config: Any, entry: Dict[str, Any]) -> Optional[str]:
    """Append one metadata-only "the user handled this object" record."""
    return _append_metadata_line(resolutions_path(config), entry)


def record_move_intent(config: Any, entry: Dict[str, Any]) -> Optional[str]:
    """Append the record written *before* a move, so a move is never untraceable."""
    return _append_metadata_line(move_intents_path(config), entry)


def cancel_move_intent(config: Any, *, retained_path: str, digest: str, note: str) -> Optional[str]:
    """Mark one move-intent line as no longer pending (append-only, never rewritten).

    A move that was undone must not leave an intent that a later run could mistake
    for a move that really happened; the cancellation is a new line that names the
    digest of the line it cancels, so the intent file is never edited in place.
    """
    entry = {
        "schema": INTENT_SCHEMA,
        "kind": "move_intent_cancelled",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retained_path": retained_path,
        "cancels": digest,
        "note": note,
    }
    return _append_metadata_line(move_intents_path(config), entry)


def read_move_intents(config: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read the move-intent record. Unreadable or corrupt input becomes a problem."""
    entries, problems = _read_metadata_lines(
        move_intents_path(config), "retained move-intent record"
    )
    accepted: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("kind") not in (
            "move_intent",
            "move_intent_cancelled",
            "captured_object",
        ):
            problems.append(
                f"{move_intents_path(config)}: a line names the kind {entry.get('kind')!r}, "
                "which this version does not know"
            )
            continue
        accepted.append(entry)
    return accepted, problems


def _cancelled_digests(entries: List[Dict[str, Any]]) -> Set[str]:
    """Digests of the move-intent lines that some cancellation line has retired."""
    return {
        str(entry["cancels"])
        for entry in entries
        if entry.get("kind") == "move_intent_cancelled" and entry.get("cancels")
    }


def _live_move_intents(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Move intents that no cancellation line has retired, oldest first."""
    cancelled = _cancelled_digests(entries)
    live: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("kind") != "move_intent":
            continue
        if _row_digest(entry) in cancelled:
            continue
        live.append(entry)
    return live


def _live_captured_objects(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Records of the objects a move captured by mistake and could not put back.

    Such an object stays exactly where the move left it; the record lets a later run
    list it after a restart instead of rediscovering it by name alone.  A cancellation
    line retires a move intent, never one of these records: the object the user still
    has to look at must stay listed until it is resolved.
    """
    return [entry for entry in entries if entry.get("kind") == "captured_object"]


def record_captured_object(
    config: Any,
    *,
    captured_path: Path,
    recorded_path: Path,
    captured_identity: Dict[str, Any],
    reason: str,
) -> Optional[str]:
    """Append the record for an object a move captured by mistake and could not put back.

    The line names the object's real kept location and the identity it had when it was
    captured, so a later run can list it after a restart instead of rediscovering it by
    name alone, and so retrieval can exclude that location.  ``retained_path`` is the
    generic "the path this record names" slot of the record (here: where the object is
    kept), not a claim that the object is a retained one.  The object itself is never
    touched again: the user handles it by hand.
    """
    entry = {
        "schema": INTENT_SCHEMA,
        "kind": "captured_object",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retained_path": str(captured_path),
        "recorded_path": str(recorded_path),
        "object_identity": captured_identity,
        "reason": reason,
        "note": (
            "an object a move captured by mistake and could not put back; it is kept exactly "
            "where it is, never deleted, never adopted as an experience and never entered into "
            "any index or retrieval; the user handles it by hand"
        ),
    }
    return _append_metadata_line(move_intents_path(config), entry)


def _read_metadata_lines(target: Path, label: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    problems: List[str] = []
    entries: List[Dict[str, Any]] = []
    try:
        info = target.lstat()
    except FileNotFoundError:
        return entries, problems
    except OSError as exc:
        problems.append(f"the {label} {target} cannot be inspected: {exc}")
        return entries, problems
    if stat.S_ISLNK(info.st_mode):
        problems.append(f"the {label} {target} is a symlink and is never followed")
        return entries, problems
    if not stat.S_ISREG(info.st_mode):
        problems.append(f"the {label} {target} is not a regular file")
        return entries, problems
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        problems.append(f"the {label} {target} cannot be read: {exc}")
        return entries, problems
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{target}: line {number} is not valid JSON: {exc}")
            continue
        if not isinstance(parsed, dict) or not parsed.get("retained_path"):
            problems.append(f"{target}: line {number} is not a {label}")
            continue
        entries.append(parsed)
    return entries, problems


def read_journal(config: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read the metadata journal. Unreadable or corrupt input becomes a problem."""
    return _read_metadata_lines(journal_path(config), "retained-object record")


def read_resolutions(config: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Read the "the user handled this object" journal."""
    entries, problems = _read_metadata_lines(
        resolutions_path(config), "retained-resolution record"
    )
    accepted: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("action") not in ("moved_to_trash", "confirmed_deleted"):
            problems.append(
                f"{resolutions_path(config)}: a resolution record names the action "
                f"{entry.get('action')!r}, which this version does not know"
            )
            continue
        accepted.append(entry)
    return accepted, problems


def _recorded_identity_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """The identity a record carries, whether it could be verified, and what that means.

    Every listed object carries both identities: ``dev``/``ino`` are what is on disk
    *right now*, and ``recorded_object_identity`` is what the record says. They are
    only equal for the object that was really retained.
    """
    recorded = row.get("object_identity")
    if isinstance(recorded, dict) and recorded.get("ino") is not None:
        identity = {"dev": recorded.get("dev"), "ino": recorded.get("ino")}
        return {
            "recorded_object_identity": identity,
            "recorded_identity_note": row.get("identity_note") or "",
            "recorded_identity_source": row.get("identity_source") or "unknown",
            "identity_verified": None,
        }
    return {
        "recorded_object_identity": None,
        "recorded_identity_note": row.get("identity_note")
        or (
            "this record carries no object identity, so the move command cannot verify that "
            "the object at this path is the recorded one; check the object yourself and then "
            "move it with --expect-dev/--expect-ino using the on-disk dev/ino this list prints"
        ),
        "recorded_identity_source": row.get("identity_source") or "unavailable",
        "identity_verified": False,
    }


def _describe_object(candidate: Path, parsed: Dict[str, Any]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "retained_path": _record_key(candidate),
        "original_path": _key(candidate.parent / parsed["original"]),
        "suffix": parsed["suffix"],
        "writer_pid": int(parsed["pid"]),
        "state": "retained_object" if parsed["suffix"] == "rollback" else "stale_staging_name",
        "task_id": "unknown",
        "operation": "unknown",
        "reason": "unknown",
        "recorded_at": "unknown",
        "written_bytes_sha256": "not_recorded",
        "manual_verification": "pending",
        "readable": True,
        "size_bytes": None,
        "mtime_ns": None,
        "mode": None,
        "dev": None,
        "ino": None,
        "content_frozen": False,
        "content_hash": "not_recorded",
        "content_copied": False,
        "journal": "missing",
        "discovered_by": "scan",
        # A scanned object has no record yet, so there is nothing to compare it to.
        "recorded_object_identity": None,
        "recorded_identity_note": "no discovery record names this object yet",
        "recorded_identity_source": "unavailable",
        "identity_verified": False,
        "problems": [],
    }
    try:
        stat_result = candidate.lstat()
    except OSError as exc:
        entry["readable"] = False
        entry["problems"].append(f"cannot inspect the retained object: {exc}")
        return entry
    if stat.S_ISLNK(stat_result.st_mode):
        entry["readable"] = False
        entry["problems"].append("the retained name is a symlink and is never followed")
        return entry
    if stat.S_ISDIR(stat_result.st_mode):
        entry["readable"] = False
        entry["problems"].append("the retained name holds a directory, not a file")
        return entry
    if not stat.S_ISREG(stat_result.st_mode):
        entry["readable"] = False
        entry["problems"].append("the retained name is not a regular file")
        return entry
    entry["size_bytes"] = stat_result.st_size
    entry["mtime_ns"] = stat_result.st_mtime_ns
    entry["mode"] = stat.S_IMODE(stat_result.st_mode)
    entry["dev"] = int(stat_result.st_dev)
    entry["ino"] = int(stat_result.st_ino)
    try:
        descriptor = os.open(str(candidate), os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        entry["readable"] = False
        entry["problems"].append(f"the retained object cannot be opened: {exc}")
    else:
        os.close(descriptor)
    return entry


def _scan_directory(directory: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    objects: List[Dict[str, Any]] = []
    problems: List[str] = []
    try:
        children = sorted(directory.iterdir())
    except FileNotFoundError:
        return objects, problems
    except OSError as exc:
        problems.append(f"retained discovery could not list {directory}: {exc}")
        return objects, problems
    for candidate in children:
        if len(objects) >= MAX_SCAN_ENTRIES:
            problems.append(
                f"retained discovery stopped after {MAX_SCAN_ENTRIES} objects in {directory}"
            )
            break
        parsed = parse_retained_name(candidate.name)
        if parsed is None:
            continue
        objects.append(_describe_object(candidate, parsed))
    return objects, problems


def scan_retained(config: Any) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Enumerate retained objects on disk. Returns ``(objects, scanned_dirs, problems)``."""
    objects: List[Dict[str, Any]] = []
    problems: List[str] = []
    scanned: List[str] = []
    for directory in retained_scan_dirs(config):
        scanned.append(str(directory))
        found, directory_problems = _scan_directory(directory)
        objects.extend(found)
        problems.extend(directory_problems)
    return objects, scanned, problems


def _explained_digests(resolutions: List[Dict[str, Any]]) -> Set[str]:
    explained: Set[str] = set()
    for item in resolutions:
        for digest in item.get("explained_record_digests") or []:
            if isinstance(digest, str):
                explained.add(digest)
    return explained


def retained_paths(config: Any) -> Set[str]:
    """Absolute paths of every retained object, from the scan *and* the journal.

    The union is deliberate: a scan hit whose record is missing is still excluded,
    and a journal record whose object is gone keeps the old path excluded *until*
    the user has handled that object.  Once a resolution explains a record, the
    record stops excluding the path -- otherwise an old record would permanently
    hide whatever new object later appeared at the same path.

    A location where a move had to keep an object it captured by mistake (a
    ``captured_object`` record) is excluded the same way: that location is a trash
    name, so the exclusion never hides a legitimate note, and it stays excluded until
    the user handles the object there.  The same holds when that record could not be
    written at all: the object then sits at the deterministic trash name derived from
    its move announcement, and that place is excluded just like a recorded one.
    """
    paths: Set[str] = set()
    objects, _, _ = scan_retained(config)
    for entry in objects:
        paths.add(_record_key(entry["retained_path"]))
    entries, _ = read_journal(config)
    resolutions, _ = read_resolutions(config)
    explained = _explained_digests(resolutions)
    for entry in entries:
        if not entry.get("retained_path"):
            continue
        if _row_digest(entry) in explained:
            continue
        paths.add(_record_key(entry["retained_path"]))
    intents, _ = read_move_intents(config)
    for entry in _live_captured_objects(intents):
        if entry.get("retained_path"):
            paths.add(_record_key(entry["retained_path"]))
    for intent in _unsettled_move_intents(intents, resolutions):
        for unrecorded in _find_unrecorded_captured_objects(config, intent):
            paths.add(_record_key(unrecorded))
    return paths


def is_retained_path(config: Any, path: Any) -> bool:
    """Isolation gate for retrieval and index entry points.

    Two independent doors: the deterministic private name rule, and the recorded or
    observed retained absolute path set.  Neither alone is the answer -- the name
    rule catches objects whose record was lost, the path set catches retained
    objects that no longer carry the private name.
    """
    candidate = Path(str(path))
    if is_retained_name(candidate.name):
        return True
    try:
        known = retained_paths(config)
    except OSError:
        return False
    if _record_key(candidate) in known:
        return True
    resolved = _key(candidate)
    return resolved != _record_key(candidate) and resolved in known


def _entry_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Describe the object a journal line names, before touching the filesystem."""
    retained_key = _record_key(row.get("retained_path"))
    parsed = parse_retained_name(Path(retained_key).name) or {}
    suffix = row.get("suffix") or parsed.get("suffix") or "unknown"
    original = row.get("original_path")
    if not original:
        original = Path(retained_key).parent / str(parsed.get("original") or "unknown")
    entry: Dict[str, Any] = {
        "retained_path": retained_key,
        "original_path": _key(original),
        "suffix": suffix,
        "writer_pid": row.get("writer_pid", parsed.get("pid", "unknown")),
        "state": "retained_object" if suffix != "restore" else "stale_staging_name",
        "task_id": row.get("task_id") or "unknown",
        "operation": row.get("operation") or "unknown",
        "reason": row.get("reason") or "unknown",
        "recorded_at": row.get("recorded_at") or "unknown",
        "written_bytes_sha256": row.get("written_bytes_sha256") or "not_recorded",
        "manual_verification": "pending",
        "readable": False,
        "size_bytes": None,
        "mtime_ns": None,
        "mode": None,
        "dev": None,
        "ino": None,
        "content_frozen": False,
        "content_hash": "not_recorded",
        "content_copied": False,
        "journal": "record_only",
        "discovered_by": "record",
        **_recorded_identity_fields(row),
        "identity_note": "",
        "problems": [],
    }
    if not parsed:
        entry["identity_note"] = (
            "the recorded path no longer carries the private retained name; the record "
            "is what identifies this object, so it stays excluded from retrieval"
        )
    return entry


def _inspect_recorded_path(entry: Dict[str, Any], path: Path) -> str:
    """``lstat`` exactly the path a record names.  Never follows a symlink.

    Returns ``"missing"``, ``"unverifiable"`` or ``"present"``; the entry carries
    the real identity of what is there.
    """
    try:
        stat_result = os.lstat(str(path))
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        entry["problems"].append(f"cannot inspect the recorded object: {exc}")
        return "unverifiable"
    if stat.S_ISLNK(stat_result.st_mode):
        entry["problems"].append("the recorded path is a symlink and is never followed")
        return "unverifiable"
    if stat.S_ISDIR(stat_result.st_mode):
        entry["problems"].append("the recorded path holds a directory, not a file")
        return "unverifiable"
    if not stat.S_ISREG(stat_result.st_mode):
        entry["problems"].append("the recorded path is not a regular file")
        return "unverifiable"
    entry["size_bytes"] = stat_result.st_size
    entry["mtime_ns"] = stat_result.st_mtime_ns
    entry["mode"] = stat.S_IMODE(stat_result.st_mode)
    entry["dev"] = int(stat_result.st_dev)
    entry["ino"] = int(stat_result.st_ino)
    recorded = entry.get("recorded_object_identity")
    if isinstance(recorded, dict) and recorded.get("ino") is not None:
        recorded_dev = recorded.get("dev")
        # Whether this really is the recorded object is decided here and reported by
        # the caller, so the listing never presents a replacement as the recorded one.
        entry["identity_verified"] = bool(
            (recorded_dev is None or int(recorded_dev) == int(stat_result.st_dev))
            and int(recorded["ino"]) == int(stat_result.st_ino)
        )
    else:
        # A record without an identity cannot be verified against anything.
        entry["identity_verified"] = False
    try:
        descriptor = os.open(str(path), os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        entry["problems"].append(f"the recorded object cannot be opened: {exc}")
        return "present"
    os.close(descriptor)
    entry["readable"] = True
    return "present"


def _verify_listed_identity(obj: Dict[str, Any], anomalies: List[Dict[str, Any]]) -> None:
    """Compare the identity a record carries with the object that is really at the path.

    A path is not an identity: the same path can be written again after the record
    was written, so an object that merely sits at the recorded path must not be
    presented as the recorded object.  When the record carries an identity and the
    object on disk is a different one, the listing says so instead of guessing.
    """
    recorded = obj.get("recorded_object_identity")
    on_disk_dev = obj.get("dev")
    on_disk_ino = obj.get("ino")
    if not isinstance(recorded, dict) or recorded.get("ino") is None:
        obj["identity_verified"] = False
        return
    if on_disk_dev is None or on_disk_ino is None:
        # There is nothing on disk to compare with; whatever is wrong there is already
        # reported as its own problem.
        obj["identity_verified"] = False
        return
    recorded_dev = recorded.get("dev")
    same = (recorded_dev is None or int(recorded_dev) == int(on_disk_dev)) and int(
        recorded["ino"]
    ) == int(on_disk_ino)
    obj["identity_verified"] = bool(same)
    if same:
        return
    detail = (
        "the object at this path is not the object the record names (recorded dev/ino "
        f"{recorded_dev}/{recorded.get('ino')}, on disk {int(on_disk_dev)}/{int(on_disk_ino)}); "
        "the path was reused after the record was written, so this listing does not present "
        "this object as the recorded one"
    )
    obj["problems"].append(detail)
    obj["journal"] = "replaced_on_disk"
    anomalies.append(
        {
            "kind": "recorded_object_replaced",
            "retained_path": obj["retained_path"],
            "detail": detail,
        }
    )


def _resolution_summary(resolutions: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    for item in reversed(resolutions):
        if _record_key(item.get("retained_path")) != key:
            continue
        return {
            "action": item.get("action"),
            "resolved_at": item.get("resolved_at"),
            "trash_path": item.get("trash_path"),
            "confirmed_by_user": bool(item.get("confirmed_by_user")),
            "object_verified_absent": bool(item.get("object_verified_absent")),
        }
    return None


def _resolution_identity(
    resolutions: List[Dict[str, Any]], key: str
) -> Optional[Dict[str, Any]]:
    """The identity the latest resolution for this path names, if it names one."""
    for item in reversed(resolutions):
        if _record_key(item.get("retained_path")) != key:
            continue
        recorded = item.get("object_identity")
        return recorded if isinstance(recorded, dict) else None
    return None


def _merge_journal(
    objects: List[Dict[str, Any]],
    journal_entries: List[Dict[str, Any]],
    anomalies: List[Dict[str, Any]],
    problems: List[str],
    *,
    resolutions: Optional[List[Dict[str, Any]]] = None,
    scope_dirs: Optional[List[str]] = None,
    resolved_objects: Optional[List[Dict[str, Any]]] = None,
) -> None:
    resolutions = list(resolutions or [])
    scope_dirs = list(scope_dirs or [])
    resolved_objects = resolved_objects if resolved_objects is not None else []
    explained = _explained_digests(resolutions)
    by_path_active: Dict[str, List[Dict[str, Any]]] = {}
    for entry in journal_entries:
        key = _record_key(entry.get("retained_path"))
        if _row_digest(entry) in explained:
            # The user handled this object; the record is explained and the path
            # is not a pending item any more.
            resolved_entry = _entry_from_row(entry)
            resolved_entry["state"] = "resolved"
            resolved_entry["manual_verification"] = "resolved"
            resolved_entry["journal"] = "resolved"
            resolved_entry["resolution"] = _resolution_summary(resolutions, key)
            summary = resolved_entry["resolution"] or {}
            # The real location and the handled object are surfaced at the top level as
            # well, so a later reader learns where the object really went without having
            # to dig into the nested summary.
            resolved_entry["resolution_action"] = summary.get("action")
            resolved_entry["resolved_at"] = summary.get("resolved_at")
            resolved_entry["trash_path"] = summary.get("trash_path")
            resolved_entry["object_identity"] = _resolution_identity(resolutions, key)
            resolved_entry["readable"] = None
            resolved_objects.append(resolved_entry)
            continue
        by_path_active.setdefault(key, []).append(entry)
    matched: Set[str] = set()
    for obj in objects:
        key = _record_key(obj["retained_path"])
        rows = by_path_active.get(key)
        if not rows:
            still_there = [
                item
                for item in resolutions
                if _record_key(item.get("retained_path")) == key
                and isinstance(item.get("object_identity"), dict)
                and item["object_identity"].get("ino") is not None
                and item["object_identity"].get("ino") == obj.get("ino")
                and item["object_identity"].get("dev") == obj.get("dev")
            ]
            if still_there:
                detail = (
                    "a resolution records this object as already handled, but the same "
                    "object is still at this path"
                )
                obj["problems"].append(detail)
                anomalies.append(
                    {"kind": "resolved_object_still_present", "retained_path": obj["retained_path"], "detail": detail}
                )
                continue
            obj["problems"].append(
                "no discovery record for this retained object; task and operation are unknown"
            )
            anomalies.append(
                {
                    "kind": "record_missing",
                    "retained_path": obj["retained_path"],
                    "detail": "the object exists on disk but no record names it",
                }
            )
            continue
        row = rows[0]
        matched.add(key)
        obj["journal"] = "matched"
        for field in ("task_id", "operation", "reason", "recorded_at", "written_bytes_sha256"):
            value = row.get(field)
            if value not in (None, ""):
                obj[field] = value
        obj.update(_recorded_identity_fields(row))
        _verify_listed_identity(obj, anomalies)
        mismatches = []
        if row.get("original_path") and _key(row["original_path"]) != _key(obj["original_path"]):
            mismatches.append(
                f"the record names the original path {row['original_path']} but the object "
                f"name says {obj['original_path']}"
            )
        if row.get("suffix") not in (None, "", obj["suffix"]):
            mismatches.append(
                f"the record names the suffix {row['suffix']} but the object name says {obj['suffix']}"
            )
        if len(rows) > 1:
            mismatches.append(f"{len(rows)} records name the same retained path")
        if mismatches:
            obj["journal"] = "mismatched"
            obj["problems"].extend(mismatches)
            anomalies.append(
                {
                    "kind": "record_mismatch",
                    "retained_path": obj["retained_path"],
                    "detail": "; ".join(mismatches),
                }
            )
    for key, rows in by_path_active.items():
        if key in matched:
            continue
        for row in rows:
            entry = _entry_from_row(row)
            if not _within_scope(key, scope_dirs):
                # A record that names a path outside the documented scope is
                # reported, never followed: this check does not walk the whole
                # filesystem on the word of one line.
                detail = (
                    "the record names a path outside the documented discovery scope; this "
                    "check reports it and does not follow it"
                )
                entry["state"] = "record_outside_scope"
                entry["problems"].append(detail)
                anomalies.append(
                    {"kind": "record_outside_scope", "retained_path": entry["retained_path"], "detail": detail}
                )
                objects.append(entry)
                continue
            status = _inspect_recorded_path(entry, Path(key))
            if status == "missing":
                detail = "a record names this retained object but nothing is there"
                entry["state"] = "record_without_object"
                entry["journal"] = "record_without_object"
                entry["problems"].append(detail)
                anomalies.append(
                    {"kind": "record_without_object", "retained_path": entry["retained_path"], "detail": detail}
                )
            elif status == "unverifiable":
                entry["state"] = "unverifiable_object"
                anomalies.append(
                    {
                        "kind": "record_object_unverifiable",
                        "retained_path": entry["retained_path"],
                        "detail": "; ".join(entry["problems"]) or "the recorded object cannot be verified",
                    }
                )
            else:
                # The record names this path and the object really is there: list
                # the real object with its real identity instead of reporting a
                # missing object.
                entry["journal"] = "record_matched_on_disk"
            objects.append(entry)
    for obj in objects:
        problems.extend(f"{obj['retained_path']}: {item}" for item in obj["problems"])
    problems.extend(f"{item['kind']}: {item.get('retained_path')}: {item['detail']}" for item in anomalies)


def _intent_object_location(config: Any, intent: Dict[str, Any]) -> Dict[str, Any]:
    """Where the announced object really is, checked on disk and never taken on trust."""
    recorded = intent.get("object_identity")
    path = intent.get("retained_path")
    at_path: Optional[str] = None
    if path:
        try:
            info = os.lstat(str(path))
        except FileNotFoundError:
            info = None
        except OSError as exc:
            return {
                "location": "unreadable",
                "path": str(path),
                "detail": f"the announced object cannot be inspected at {path}: {exc}",
            }
        if info is not None and _identity_matches(recorded, info):
            return {
                "location": "still_at_recorded_path",
                "path": str(path),
                "detail": (
                    f"a move of {path} was announced but the object is still there, so the move "
                    "did not happen; nothing was recorded as handled, and running the resolve "
                    "command again retires the announcement"
                ),
            }
        if info is not None:
            at_path = f"{path} now holds a different object"
    found = _find_in_trash(config, intent)
    if found is not None:
        return {
            "location": "in_trash",
            "path": str(found),
            "detail": (
                f"a move of {path or 'the announced object'} was announced and the object really "
                f"is in the trash at {found}, but no resolution record names it"
                + (f"; {at_path}" if at_path else "")
                + "; running the same resolve command again appends the missing record"
            ),
        }
    if at_path:
        return {
            "location": "path_reused",
            "path": str(path),
            "detail": (
                f"a move of {path} was announced, {at_path}, and the announced object cannot be "
                "found in the trash by its recorded identity; nothing was recorded as handled"
            ),
        }
    return {
        "location": "unaccounted",
        "path": None,
        "detail": (
            f"a move of {path or 'an object'} was announced, but the announced object is neither "
            "at its recorded path nor in the trash by its recorded identity; nothing was recorded "
            "as handled and no object is adopted by name alone"
        ),
    }


def _unsettled_move_intents(
    intents: List[Dict[str, Any]], resolutions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Live announcements that no resolution record has settled yet.

    A live announcement is the trace of a move that was started.  It stays visible until a
    resolution record names the very object it announced, so a move that succeeded while
    its record could not be written is never silently forgotten.  An announcement whose
    object a resolution already names is bookkeeping that has been settled.
    """
    settled: Set[Tuple[str, int]] = set()
    for item in resolutions:
        recorded = item.get("object_identity")
        if item.get("action") != "moved_to_trash" or not isinstance(recorded, dict):
            continue
        try:
            settled.add((_record_key(item.get("retained_path")), int(recorded["ino"])))
        except (TypeError, ValueError):
            continue
    pending: List[Dict[str, Any]] = []
    for intent in _live_move_intents(intents):
        recorded = intent.get("object_identity")
        try:
            ino = int(recorded["ino"]) if isinstance(recorded, dict) else None
        except (TypeError, ValueError):
            ino = None
        if ino is not None and (_record_key(intent.get("retained_path")), ino) in settled:
            continue
        pending.append(intent)
    return pending


def _move_intents_view(
    config: Any, intents: List[Dict[str, Any]], resolutions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Move announcements that no resolution record has settled yet, with real locations."""
    views: List[Dict[str, Any]] = []
    for intent in _unsettled_move_intents(intents, resolutions):
        recorded = intent.get("object_identity")
        location = _intent_object_location(config, intent)
        views.append(
            {
                "retained_path": intent.get("retained_path"),
                "announced_at": intent.get("recorded_at"),
                "object_identity": recorded,
                "original_path": intent.get("original_path"),
                "task_id": intent.get("task_id"),
                "operation": intent.get("operation"),
                "reason": intent.get("reason"),
                "object_location": location["location"],
                "object_location_path": location["path"],
                "detail": location["detail"],
            }
        )
    return views


def _captured_objects_view(
    config: Any, intents: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Every object a move captured by mistake and could not put back, with its location now.

    The identity recorded at capture time is compared with what is at the kept
    location now: an object the user is still looking for must stay visible even if
    it was renamed, moved or deleted afterwards.
    """
    views: List[Dict[str, Any]] = []
    for entry in _live_captured_objects(intents):
        kept = entry.get("retained_path")
        found = _intent_object_location(config, entry)
        location = found["location"]
        if location == "still_at_recorded_path":
            detail = (
                f"the object captured by mistake is still exactly where the move left it "
                f"({kept}); the user handles it by hand"
            )
        elif location == "in_trash":
            detail = (
                f"the object captured by mistake is kept in the trash at {found['path']}, "
                "exactly where the move left it; the user handles it by hand"
            )
        elif location == "unreadable":
            detail = (
                f"the object captured by mistake cannot be inspected at {kept}, so it is listed "
                "here rather than being rediscovered by name alone"
            )
        elif location == "path_reused":
            detail = (
                f"the location where the move kept the captured object ({kept}) now holds a "
                "different object, so the captured object has to be looked for by its recorded "
                "identity; nothing was deleted"
            )
        else:
            detail = (
                f"the object captured by mistake is no longer at the location the record names "
                f"({kept}) and cannot be found there by its recorded identity; nothing was "
                "deleted"
            )
        views.append(
            {
                "captured_path": kept,
                "recorded_path": entry.get("recorded_path"),
                "captured_at": entry.get("recorded_at"),
                "object_identity": entry.get("object_identity"),
                "reason": entry.get("reason"),
                "location_now": location,
                "location_now_path": found["path"],
                "detail": detail,
            }
        )
    return views


def _unrecorded_captured_objects_view(
    config: Any, intents: List[Dict[str, Any]], resolutions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Captured objects that no record names, found by the names their announcements account for.

    A move that captured the wrong object and could not put it back tries to write the
    record that lists it.  When that write itself fails, nothing names the object any more --
    so the move leaves it at a trash name derived from the announcement it wrote *before*
    the move and from the object's own inode, and this view rebuilds that location after a
    restart.  Every name it accepts is checked against the object found there: the object
    must really carry the inode the name claims, so a file that merely owns one of these
    names is never listed as a capture.  Each object is listed with its real identity and the
    reason it is there, and retrieval excludes the place.
    """
    views: List[Dict[str, Any]] = []
    for intent in _unsettled_move_intents(intents, resolutions):
        for found in _find_unrecorded_captured_objects(config, intent):
            try:
                info = os.lstat(str(found))
            except OSError:  # pragma: no cover - the object was just inspected
                continue
            reason = (
                "no record names this object, and it sits under a trash name that this move's "
                "announcement accounts for and that the object itself confirms by carrying the "
                "inode the name claims, so the move that captured it could not write the record "
                "that would have listed it"
            )
            detail = (
                f"an object is kept in the trash at {found} under a name accounted for by the "
                f"announced move of {intent.get('retained_path')}; the name was derived from that "
                "move's announcement and from the object's own inode, which is what this check "
                "verified the object against, because no record names this place; the user "
                "handles it by hand"
            )
            views.append(
                {
                    "captured_path": str(found),
                    "recorded_path": intent.get("retained_path"),
                    "captured_at": intent.get("recorded_at"),
                    "object_identity": _identity_of(info),
                    "reason": reason,
                    "location_now": "in_trash",
                    "location_now_path": str(found),
                    "detail": detail,
                }
            )
    return views


def collect(config: Any) -> Dict[str, Any]:
    """The self-check view: every retained object plus every discovery anomaly."""
    objects, scanned, scan_problems = scan_retained(config)
    journal_entries, journal_problems = read_journal(config)
    resolutions, resolution_problems = read_resolutions(config)
    intents, intent_problems = read_move_intents(config)
    anomalies: List[Dict[str, Any]] = []
    problems = (
        list(scan_problems)
        + list(journal_problems)
        + list(resolution_problems)
        + list(intent_problems)
    )
    resolved_objects: List[Dict[str, Any]] = []
    _merge_journal(
        objects,
        journal_entries,
        anomalies,
        problems,
        resolutions=resolutions,
        scope_dirs=scanned,
        resolved_objects=resolved_objects,
    )
    # A move that was announced but never settled by a resolution record is the trace
    # of a move that may have happened while its record could not be written, so it is
    # reported with the real location of the object it names.
    intent_views = _move_intents_view(config, intents, resolutions)
    anomalies.extend(
        {
            "kind": "move_intent_without_resolution",
            "retained_path": view["retained_path"],
            "detail": view["detail"],
        }
        for view in intent_views
    )
    # An object this version captured by mistake and could not put back stays in the
    # trash and is recorded there with the identity it had at capture time.  The
    # self-check lists it after every restart, so it is never rediscovered by name
    # alone and its content is never adopted.
    captured_views = _captured_objects_view(config, intents)
    # When even that record could not be written, no line names the object; the move then
    # keeps it at the deterministic trash name derived from the move announcement, and
    # this check finds it again from that announcement after every restart.  A location
    # already named by a record is never listed twice.
    known_captured = {_record_key(view["captured_path"]) for view in captured_views}
    for view in _unrecorded_captured_objects_view(config, intents, resolutions):
        if _record_key(view["captured_path"]) in known_captured:
            continue
        known_captured.add(_record_key(view["captured_path"]))
        captured_views.append(view)
    anomalies.extend(
        {
            "kind": "captured_object_kept",
            "retained_path": view["captured_path"],
            "detail": view["detail"],
        }
        for view in captured_views
    )
    # An object this check could not verify (unreadable, a symlink, a directory)
    # means the enumeration did not account for everything it found, exactly like
    # a missing or mismatched record does.  The same problems are already in the
    # list above, so nothing is reported twice.
    object_problems = [
        f"{obj['retained_path']}: {item}" for obj in objects for item in obj["problems"]
    ]
    objects.sort(key=lambda item: (str(item["retained_path"]),))
    resolved_objects.sort(key=lambda item: (str(item["retained_path"]),))
    retained = [item for item in objects if item["state"] in RETAINED_OBJECT_STATES]
    payload: Dict[str, Any] = {
        "schema": JOURNAL_SCHEMA,
        "ok": not objects
        and not anomalies
        and not scan_problems
        and not journal_problems
        and not resolution_problems
        and not intent_problems,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retained_count": len(retained),
        "object_count": len(objects),
        "pending_count": len(objects),
        "resolved_count": len(resolved_objects),
        "resolution_record_count": len(resolutions),
        "move_intent_record_count": len(intents),
        "pending_move_intent_count": len(intent_views),
        "captured_object_record_count": len(
            [entry for entry in intents if entry.get("kind") == "captured_object"]
        ),
        "captured_object_count": len(captured_views),
        "anomaly_count": len(anomalies),
        "journal_path": str(journal_path(config)),
        "resolutions_path": str(resolutions_path(config)),
        "move_intents_path": str(move_intents_path(config)),
        "report_path": str(report_path(config)),
        "discovery_scope": scanned,
        "journal_scope": str(journal_path(config)),
        "discovery_note": (
            "filesystem scan of the directories above is authoritative; the metadata "
            "record only supplies task/operation/reason and is checked both ways. A "
            "record that names a path outside the scope above is reported and not "
            "followed, so this check never becomes an unbounded filesystem walk"
        ),
        "content_policy": (
            "metadata only: no content is copied and no hash freezes an object that may "
            "still be written through file descriptors opened before the claim"
        ),
        "cleanup": {
            "automatic": False,
            "expires": None,
            "note": (
                "retained objects are never deleted automatically, never expire, and are "
                "not removed by a later recovery, restart or release; the user authorises "
                "cleanup separately after seeing this list"
            ),
        },
        "resolution_note": (
            "an object is listed as pending until the user runs memory-retained-resolve "
            "for that one object in their own terminal; a resolved record is bookkeeping "
            "only -- its content is never adopted as an experience and never enters any "
            "index or retrieval"
        ),
        "move_intent_note": (
            "a move is announced in the move-intent record before it starts; a pending "
            "announcement means a move was started and no resolution record has settled it "
            "yet, and it is reported with the real location of the object it names"
        ),
        "captured_object_note": (
            "when a move captured an object that is not the one the record names and could not "
            "put it back, the object is kept at its real place in the trash and that place is "
            "recorded; this check lists the record after every restart with the identity captured "
            "and what is at that place now, and retrieval excludes that place"
        ),
        "manual_action_required": bool(objects) or bool(anomalies) or bool(problems),
        "reconciliation_required": bool(objects) or bool(anomalies) or bool(problems),
        "enumeration_ok": not scan_problems
        and not journal_problems
        and not resolution_problems
        and not intent_problems
        and not anomalies
        and not object_problems,
        "enumeration_note": (
            "enumeration_ok means this self-check accounted for every retained object it "
            "found: it is true even when objects are retained, and false when a record is "
            "missing, mismatched or unreadable"
        ),
        "objects": objects,
        "resolved_objects": resolved_objects,
        "move_intents": intent_views,
        "captured_objects": captured_views,
        "anomalies": anomalies,
        "problems": problems,
    }
    return payload


def write_report(config: Any, payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort JSON report; returns ``(path_or_None, error_or_None)``.

    A report that cannot be written never changes what happened to the objects: they
    are already retained, and the caller still prints their real absolute locations.

    Ownership is explicit: the temporary name is created with ``O_CREAT|O_EXCL`` (so
    a name that is already taken is never truncated or replaced, whatever it is), the
    descriptor's identity is checked against the name before the rename, and a failed
    write only ever removes the very object this call created.  The report path itself
    is replaced only when it holds a regular file, i.e. a previous report.
    """
    target = report_path(config)
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    directory_error = _ensure_metadata_directory(target.parent)
    if directory_error is not None:
        return None, f"{target}: {directory_error}"
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        return None, f"{target}: {exc}"
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        return None, (
            f"{target}: the report path holds a symlink or a non-regular object; this "
            "check never replaces an object it did not create"
        )
    descriptor: Optional[int] = None
    temporary: Optional[Path] = None
    for attempt in range(MAX_REPORT_TEMP_ATTEMPTS):
        candidate = target.parent / f".{target.name}.{os.getpid()}.{attempt}.tmp"
        try:
            descriptor = os.open(
                str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW, 0o600
            )
        except FileExistsError:
            # Somebody else's object already owns this name.  Leave it exactly as
            # it is and try the next name.
            continue
        except OSError as exc:
            return None, f"{target}: {exc}"
        temporary = candidate
        break
    if descriptor is None or temporary is None:
        return None, (
            f"{target}: no free temporary name after {MAX_REPORT_TEMP_ATTEMPTS} attempts"
        )
    identity = None
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
        identity = os.fstat(descriptor)
        current = os.lstat(str(temporary))
        if not stat.S_ISREG(current.st_mode) or (
            int(current.st_dev),
            int(current.st_ino),
        ) != (int(identity.st_dev), int(identity.st_ino)):
            raise OSError(
                f"the temporary report name {temporary} no longer holds the object this "
                "call created"
            )
        os.close(descriptor)
        descriptor = None
        os.replace(str(temporary), str(target))
        temporary = None
    except OSError as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            _remove_own_object(temporary, identity)
        return None, f"{target}: {exc}"
    return str(target), None


def _remove_own_object(candidate: Path, identity: Any) -> bool:
    """Unlink ``candidate`` only if it is still the very object this call created."""
    try:
        current = os.lstat(str(candidate))
    except OSError:
        return False
    if not stat.S_ISREG(current.st_mode):
        return False
    if identity is not None and (int(current.st_dev), int(current.st_ino)) != (
        int(identity.st_dev),
        int(identity.st_ino),
    ):
        return False
    try:
        os.unlink(str(candidate))
    except OSError:
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _same_object(info: Any, other: Any) -> bool:
    """Is the object behind ``other`` the very object described by ``info``?

    Only the physical identity is compared.  A writer that still holds an open
    descriptor can change size and mtime of the very object, and it is still the
    same object.
    """
    if info is None or other is None or not stat.S_ISREG(other.st_mode):
        return False
    return (int(other.st_dev), int(other.st_ino)) == (int(info.st_dev), int(info.st_ino))


def _identity_matches(recorded: Any, info: Any) -> bool:
    """Does the object on disk carry exactly the recorded identity?

    A record written by an older version may name no device; the inode alone is
    then the only physical fact available, and it is still compared.
    """
    if not isinstance(recorded, dict) or recorded.get("ino") is None or info is None:
        return False
    try:
        if recorded.get("dev") is not None and int(recorded["dev"]) != int(info.st_dev):
            return False
        return int(recorded["ino"]) == int(info.st_ino)
    except (TypeError, ValueError):
        return False


def _find_in_trash(config: Any, intent: Dict[str, Any]) -> Optional[Path]:
    """Find the announced object in the trash by its exact identity, never by name alone."""
    directory = trash_dir(config)
    original = intent.get("original_path") or intent.get("retained_path") or "unknown"
    base = Path(str(original)).name
    for attempt in range(MAX_TRASH_NAME_ATTEMPTS):
        name = base if attempt == 0 else f"{base}.{attempt}"
        try:
            info = os.lstat(str(directory / name))
        except OSError:
            continue
        if _identity_matches(intent.get("object_identity"), info):
            return directory / name
    return None


def _recorded_inode(identity: Any) -> Optional[int]:
    """The inode a recorded identity names, or None when it names none."""
    if not isinstance(identity, dict):
        return None
    try:
        return int(identity["ino"])
    except (KeyError, TypeError, ValueError):
        return None


def _captured_kept_name(recorded_identity: Any, captured_identity: Any, base: str) -> Optional[str]:
    """The trash name a captured object no record could name is kept under.

    The name carries both inodes: the one the move announcement recorded for the object
    the move was for, and the one of the object the move actually captured.  The second is
    what makes the name checkable from a later process, which has no record of the captured
    object at all: an object at such a name counts as the kept capture only when it really
    carries the inode the name claims, so an unrelated file that happens to sit at a name
    this mechanism uses can never be reported as the captured object.  Returns None when
    either identity carries no inode.
    """
    announced = _recorded_inode(recorded_identity)
    captured = _recorded_inode(captured_identity)
    if announced is None or captured is None:
        return None
    return f"{CAPTURED_KEPT_PREFIX}{announced}.{captured}.{base}"


def _keep_unrecorded_captured_object(
    directory: Path, candidate: Path, base: str, recorded_info: Any
) -> Tuple[Optional[Path], Optional[str]]:
    """Keep a captured object at the deterministic name a later run can reconstruct.

    Used only when the record that would have named the object could not be written: the
    move announcement is then the only thing on disk that describes the object, so the
    object has to sit where that announcement -- together with the object's own identity,
    which only this process can still read -- says it can be found again.  The name carries
    both, and a later run checks the object it finds against the name, so the same place is
    neither guessed nor confused with an unrelated file that owns the first free name.  The
    move is the same exclusive directory-entry move as everywhere else, so a name that is
    already taken is never overwritten.  Returns ``(kept_path, error)``; ``error`` names the
    real failure when the object could not be put on such a name.
    """
    try:
        captured_info = os.lstat(str(candidate))
    except OSError as exc:
        return None, f"the captured object at {candidate} could not be inspected: {exc}"
    name = _captured_kept_name(_identity_of(recorded_info), _identity_of(captured_info), base)
    if name is None:
        return None, (
            "the move announcement or the captured object carries no inode, so no checkable "
            "name can be derived from them"
        )
    for attempt in range(MAX_TRASH_NAME_ATTEMPTS):
        target = directory / (name if attempt == 0 else f"{name}.{attempt}")
        moved, code = _rename_exclusive(candidate, target)
        if moved:
            _fsync_directory(directory)
            return target, None
        if code == errno.EEXIST:
            continue
        return None, f"{candidate} -> {target}: {os.strerror(code)}"
    return (
        None,
        f"no free name for {name} in {directory} after {MAX_TRASH_NAME_ATTEMPTS} attempts",
    )


def _find_unrecorded_captured_objects(config: Any, intent: Dict[str, Any]) -> List[Path]:
    """Find captured objects no record names, under the names their announcement accounts for.

    The move announcement is what a later run has: it names the object the move was for and
    the trash directory.  Every kept name for that announcement starts with the announced
    inode and carries, as its next part, the inode of the object that was really captured.
    An object found at such a name counts only when it is a regular file that really carries
    the inode its own name claims and is not the announced object itself: the name is the
    check, which is why an unrelated file that owns one of these names is never reported as
    the capture.  Every name that passes the check is returned, so a name that was already
    taken when the object was kept cannot hide the object kept under the next one.  Only this
    one trash directory is read, and only names in this one namespace are looked at; ordinary
    files in the real trash are never swept up.
    """
    announced = _recorded_inode(intent.get("object_identity"))
    if announced is None:
        return []
    directory = trash_dir(config)
    marker = f"{CAPTURED_KEPT_PREFIX}{announced}."
    try:
        with os.scandir(directory) as scan:
            names = sorted(entry.name for entry in scan)
    except OSError:
        return []
    found: List[Path] = []
    for name in names:
        if not name.startswith(marker):
            continue
        claimed = name[len(marker):].partition(".")[0]
        if not claimed.isdigit():
            continue
        location = directory / name
        try:
            info = os.lstat(str(location))
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if int(info.st_ino) == int(claimed) and int(info.st_ino) != announced:
            found.append(location)
    return found


def _rename_exclusive(source: Path, target: Path) -> Tuple[bool, Optional[int]]:
    """Move the directory entry ``source`` onto ``target``, never replacing anything.

    Returns ``(moved, errno_code)``.  ``EEXIST`` means something already owns the
    target name (ordinary when another writer is racing for the same name); ``ENOENT``
    means the source name is already gone.  Any other error is returned as-is for the
    caller to report.  Without the platform primitive the move is refused
    (``ENOSYS``): a plain ``os.rename`` would overwrite whatever object appeared at
    the target between the check and the move, so it is never a stand-in.
    """
    if _EXCLUSIVE_MOVE is None:
        return False, errno.ENOSYS
    ctypes.set_errno(0)
    result = _EXCLUSIVE_MOVE(os.fsencode(str(source)), os.fsencode(str(target)), _RENAME_EXCL)
    if result == 0:
        return True, None
    return False, ctypes.get_errno()


def _move_into_trash(
    config: Any, source: Path, info: Any
) -> Tuple[Optional[Path], Optional[str], Optional[Dict[str, Any]]]:
    """Move the object into the system trash, keeping the same inode and never by name.

    The recorded name is taken away with a single *exclusive* directory-entry move
    (``renamex_np`` with ``RENAME_EXCL``): the kernel refuses to apply it onto a name
    that is already taken, so there is no check-then-move window, no staging name is
    created, and no step of this chain unlinks anything.  The object the move actually
    captured is then compared by identity with the recorded one; when they differ, that
    object is put back with a second exclusive move, or -- if the recorded path has been
    taken again meanwhile -- keeps its real place in the trash and is recorded there
    (``captured_object``), never deleted.

    Returns ``(trash_path, error, conflict)``; at most one of the three is set.
    """
    directory = trash_dir(config)
    try:
        dir_info = directory.lstat()
    except OSError as exc:
        return None, f"the system trash {directory} cannot be inspected: {exc}", None
    if stat.S_ISLNK(dir_info.st_mode):
        return (
            None,
            f"the system trash {directory} is a symlink; refusing to move an object through it",
            None,
        )
    if not stat.S_ISDIR(dir_info.st_mode):
        return None, f"the system trash {directory} is not a directory", None
    if int(dir_info.st_dev) != int(info.st_dev):
        return (
            None,
            (
                f"the system trash {directory} is on another filesystem "
                f"(device {dir_info.st_dev} != {info.st_dev}); this command moves an object "
                "and never copies one across filesystems"
            ),
            None,
        )
    parsed = parse_retained_name(source.name) or {}
    preferred = str(parsed.get("original") or source.name)
    candidate: Optional[Path] = None
    for attempt in range(MAX_TRASH_NAME_ATTEMPTS):
        name = preferred if attempt == 0 else f"{preferred}.{attempt}"
        target = directory / name
        moved, code = _rename_exclusive(source, target)
        if moved:
            candidate = target
            break
        if code == errno.EEXIST:
            # The kernel refused to apply the move onto a taken name; try the next one.
            continue
        if code == errno.ENOENT:
            return (
                None,
                (
                    f"{source}: the recorded name was already gone when the move was attempted; "
                    "nothing was moved, nothing was created and nothing was recorded as handled"
                ),
                None,
            )
        if code == errno.ENOSYS:
            return (
                None,
                (
                    "this platform has no exclusive directory-entry move (renamex_np with "
                    "RENAME_EXCL) and a plain rename would overwrite whatever appeared at the "
                    "target; this command never falls back to a move that can destroy an object, "
                    "so nothing was moved"
                ),
                None,
            )
        return None, f"{source} -> {target}: {os.strerror(code)}", None
    if candidate is None:
        return (
            None,
            (
                f"no free name for {preferred} in {directory} after "
                f"{MAX_TRASH_NAME_ATTEMPTS} attempts; nothing was moved"
            ),
            None,
        )
    try:
        captured = os.lstat(str(candidate))
    except OSError as exc:
        return (
            None,
            (
                f"{candidate}: {exc}; the recorded name was moved to {candidate} but the object "
                "there could not be inspected, so nothing was recorded as handled and the object "
                "stays exactly where this move left it"
            ),
            None,
        )
    if not _same_object(info, captured):
        # The exclusive move captured an object that is not the recorded one.  It is put
        # back the same way it was taken; when the recorded path has been taken again in
        # the meantime it keeps its real place in the trash and is recorded there, and it
        # is never deleted.
        put_back, put_back_code = _rename_exclusive(candidate, source)
        captured_fields = {
            "kind": "recorded_path_changed_before_move",
            "recorded_path": str(source),
            "recorded_object_preserved_at": None,
            "captured_object_restored": bool(put_back),
        }
        if put_back:
            _fsync_directory(source.parent)
            _fsync_directory(directory)
            captured_fields["captured_object_path"] = str(source)
            captured_fields["detail"] = (
                f"{source} did not hold the object the record names when the recorded name was "
                "moved away, and the object that was there has been put back on it untouched; "
                "nothing was recorded as handled and no object was overwritten or destroyed"
            )
            return None, None, captured_fields
        reason = (
            "the recorded path was taken again before the captured object could be put back "
            f"({os.strerror(put_back_code) if put_back_code is not None else 'unknown error'})"
        )
        record_error = record_captured_object(
            config,
            captured_path=candidate,
            recorded_path=source,
            captured_identity=_identity_of(captured),
            reason=reason,
        )
        kept = candidate
        keep_error: Optional[str] = None
        if record_error is not None:
            # No record names this object now, so it has to sit where a later run can find it
            # again from what is already on disk: the announcement written *before* the move,
            # plus the object's own inode, which only this process can still read.  Both go
            # into the name, so the next run checks the object it finds against that name
            # instead of believing the first free name it comes across.
            parsed = parse_retained_name(source.name) or {}
            base = str(parsed.get("original") or source.name)
            kept_name, keep_error = _keep_unrecorded_captured_object(
                directory, candidate, base, info
            )
            if kept_name is not None:
                kept = kept_name
        derived = kept != candidate
        captured_state = (
            f"the object that was at the recorded path could not be put back ({reason}); it is "
            f"kept exactly at {kept}"
        )
        if record_error is not None:
            captured_state += (
                f", and the record that would list it could not be written ({record_error})"
            )
            if derived:
                captured_state += (
                    ", so it was left under the trash name that carries this move's announcement "
                    "and the object's own inode, where the next run checks the object it finds "
                    "and so finds it again without any record"
                )
            else:
                captured_state += ", so this conflict is the only note of where it is kept"
        if keep_error is not None:
            captured_state += (
                f"; it could not be put on that name either ({keep_error})"
            )
        captured_fields["captured_object_path"] = str(kept)
        captured_fields["record_error"] = record_error
        captured_fields["captured_object_kept_under_derived_name"] = derived
        captured_fields["keep_error"] = keep_error
        captured_fields["detail"] = (
            f"{source} did not hold the object the record names when the recorded name was moved "
            f"away, so {captured_state}; nothing was recorded as handled and no object was "
            "overwritten or destroyed"
        )
        return None, None, captured_fields
    _fsync_directory(directory)
    _fsync_directory(source.parent)
    return candidate, None, None


def _resolution_entry(
    *,
    key: str,
    action: str,
    rows: List[Dict[str, Any]],
    object_identity: Optional[Dict[str, Any]],
    trash_path: Optional[str],
    confirmed_by_user: bool,
    object_verified_absent: bool,
    explained: List[str],
    note: str,
) -> Dict[str, Any]:
    row = rows[0] if rows else {}
    parsed = parse_retained_name(Path(key).name) or {}
    original = row.get("original_path") or str(
        Path(key).parent / str(parsed.get("original") or "unknown")
    )
    return {
        "schema": RESOLUTION_SCHEMA,
        "kind": "retained_resolution",
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retained_path": key,
        "original_path": _key(original),
        "suffix": row.get("suffix") or parsed.get("suffix") or "unknown",
        "writer_pid": row.get("writer_pid", parsed.get("pid", "unknown")),
        "task_id": row.get("task_id") or "unknown",
        "operation": row.get("operation") or "unknown",
        "reason": row.get("reason") or "unknown",
        "action": action,
        "object_identity": object_identity,
        "trash_path": trash_path,
        "object_verified_absent": bool(object_verified_absent),
        "confirmed_by_user": bool(confirmed_by_user),
        "explained_record_digests": sorted(set(explained)),
        "content_copied": False,
        "adopted_as_experience": False,
        "note": note,
    }


def _identity_of(info: Any) -> Dict[str, Any]:
    return {
        "dev": int(info.st_dev),
        "ino": int(info.st_ino),
        "size_bytes": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "mode": int(stat.S_IMODE(info.st_mode)),
    }


def _confirm_deleted(
    config: Any,
    outcome: Dict[str, Any],
    key: str,
    info: Any,
    rows_for_path: List[Dict[str, Any]],
    prior: List[Dict[str, Any]],
    named_by_rule: bool,
    intents: Optional[List[Dict[str, Any]]] = None,
    resolutions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Record the user's own deletion; refuse while the object is still there.

    A path with no discovery record may still be confirmed when the private name
    rule matches it and this path has handled history: that is the documented way
    out for an object that appeared at an already-handled path (a replacement),
    which the move action refuses to touch.

    A move announcement that no resolution has settled is never answered with this
    confirmation: if the announced object really is in the trash, the missing record
    is what is due, not a claim that the user deleted it.  When the announced object
    cannot be found at all, the user's own confirmation is recorded and the
    announcement is retired as unaccounted -- it is never rewritten as a move.
    """
    prior_handled = [
        item for item in prior if item.get("action") in ("moved_to_trash", "confirmed_deleted")
    ]
    if info is not None:
        outcome["status"] = "object_still_present"
        outcome["issues"].append(
            f"{key} still exists, so this command refuses to record it as deleted; delete "
            "it yourself first, then run the same command again to record that you did"
        )
        outcome["retry"] = (
            "Delete the object yourself, then rerun the same command with --confirm-deleted."
        )
        return outcome
    announced = [
        item
        for item in _unsettled_move_intents(intents or [], resolutions or [])
        if _record_key(item.get("retained_path")) == key
    ]
    for item in announced:
        found = _find_in_trash(config, item)
        if found is None:
            continue
        outcome["status"] = "move_announcement_pending"
        outcome["trash_path"] = str(found)
        outcome["issues"].append(
            f"a move of this path was announced and the object it names really is in the trash "
            f"at {found}; recording this path as deleted by hand would be a claim this command "
            "can see is not what happened"
        )
        outcome["retry"] = (
            "Run the same command with --trash to append the missing record for the object that "
            "is in the trash; --confirm-deleted is only for an object you deleted yourself."
        )
        return outcome
    for item in reversed(prior_handled):
        if item.get("action") == "confirmed_deleted":
            outcome["ok"] = True
            outcome["status"] = "already_resolved"
            outcome["resolved_at"] = item.get("resolved_at")
            outcome["note"] = "this path was already confirmed deleted; nothing was changed"
            return outcome
        if item.get("action") == "moved_to_trash":
            if named_by_rule:
                # The private name still fits this path, so what the user deleted may be
                # a replacement object that appeared after the move; the earlier record
                # must not smother it.  Fall through and record this confirmation
                # instead of claiming there is nothing new to confirm.
                break
            outcome["ok"] = True
            outcome["status"] = "already_resolved"
            outcome["trash_path"] = item.get("trash_path")
            outcome["resolved_at"] = item.get("resolved_at")
            outcome["note"] = (
                "this object was already moved to the system trash by an earlier command; "
                "the path is resolved and nothing was changed"
            )
            return outcome
    if not rows_for_path and not (named_by_rule and prior_handled):
        outcome["status"] = "not_recorded"
        outcome["issues"].append(
            "no discovery record names this path, so this command has nothing it may "
            "confirm; it only ever confirms an object the discovery record knows about"
        )
        outcome["retry"] = (
            "Look up the recorded retained path with memory-retained-list first."
        )
        return outcome
    entry = _resolution_entry(
        key=key,
        action="confirmed_deleted",
        rows=rows_for_path,
        object_identity=None,
        trash_path=None,
        confirmed_by_user=True,
        object_verified_absent=True,
        explained=[_row_digest(row) for row in rows_for_path],
        note=(
            "the user confirmed in their own terminal that this retained object is gone; "
            "the object was verified absent by lstat before this record was appended, and "
            "no content was copied, adopted as an experience, or indexed"
        ),
    )
    error = record_resolution(config, entry)
    if error is not None:
        outcome["status"] = "not_recorded"
        outcome["issues"].append(error)
        outcome["retry"] = (
            "Make the metadata directory writable, then run the same command again."
        )
        return outcome
    # The user's own confirmation is recorded; an announcement whose object was never
    # found in the trash is retired as unaccounted, and it is never rewritten as a move.
    for item in announced:
        cancel_error = cancel_move_intent(
            config,
            retained_path=key,
            digest=_row_digest(item),
            note=(
                "retired by --confirm-deleted: the user confirmed in their own terminal that this "
                "object is gone, and it was never found in the trash by its recorded identity, so "
                "no move is claimed"
            ),
        )
        if cancel_error is not None:
            outcome["issues"].append(cancel_error)
    outcome["ok"] = True
    outcome["status"] = "confirmed_deleted"
    outcome["resolved_at"] = entry["resolved_at"]
    return outcome


def _complete_moved_record(
    config: Any,
    outcome: Dict[str, Any],
    key: str,
    rows: List[Dict[str, Any]],
    intents: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Finish the record of a move that really happened but could not be recorded.

    The evidence is the announcement written before the move plus the object found in
    the trash by its exact recorded identity.  Nothing is guessed: an object in the
    trash with a different identity is never adopted, and an announcement whose object
    cannot be found is reported rather than resolved.
    """
    for intent in _live_move_intents(intents):
        if _record_key(intent.get("retained_path")) != key:
            continue
        found = _find_in_trash(config, intent)
        if found is None:
            outcome["issues"].append(
                "a move of this path was announced, but the object it names cannot be found in "
                f"the trash by its recorded identity ({intent.get('object_identity')}); nothing "
                "was recorded as handled and no same-named object was adopted"
            )
            continue
        try:
            found_info = os.lstat(str(found))
        except OSError as exc:
            outcome["issues"].append(f"{found}: {exc}")
            continue
        entry = _resolution_entry(
            key=key,
            action="moved_to_trash",
            rows=rows,
            object_identity=_identity_of(found_info),
            trash_path=str(found),
            confirmed_by_user=True,
            object_verified_absent=True,
            explained=[_row_digest(row) for row in rows],
            note=(
                "the record was appended by a later run of the same command: the move had already "
                "happened and its resolution record could not be written at the time; the object "
                "was found in the trash by its exact recorded identity, and no content was copied, "
                "adopted as an experience, or indexed"
            ),
        )
        error = record_resolution(config, entry)
        if error is not None:
            outcome["status"] = "moved_but_not_recorded"
            outcome["trash_path"] = str(found)
            outcome["issues"].append(error)
            outcome["issues"].append(
                f"the object really is at {found} now, but the resolution record still could not "
                "be written, so the list keeps showing this path as pending"
            )
            outcome["retry"] = (
                "Make the metadata directory writable, then run the same command again to append "
                "the record."
            )
            return outcome
        outcome["ok"] = True
        outcome["status"] = "moved_to_trash"
        outcome["trash_path"] = str(found)
        outcome["resolved_at"] = entry["resolved_at"]
        outcome["recorded_after_move"] = True
        outcome["note"] = (
            "the move had already happened; this run appended the missing record for the object it "
            "found in the trash, and it moved or copied nothing itself"
        )
        return outcome
    return None


def _move_to_trash(
    config: Any,
    outcome: Dict[str, Any],
    key: str,
    source: Path,
    info: Any,
    parsed: Optional[Dict[str, Any]],
    active_rows: List[Dict[str, Any]],
    prior: List[Dict[str, Any]],
    in_scope: bool,
    scope_dirs: List[str],
    expected_identity: Optional[Dict[str, Any]] = None,
    intents: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if info is None:
        for item in reversed(prior):
            if item.get("action") in ("moved_to_trash", "confirmed_deleted"):
                outcome["ok"] = True
                outcome["status"] = "already_resolved"
                outcome["trash_path"] = item.get("trash_path")
                outcome["resolved_at"] = item.get("resolved_at")
                outcome["note"] = "this path is already resolved; there is no object left to move"
                return outcome
        completed = _complete_moved_record(config, outcome, key, active_rows, intents or [])
        if completed is not None:
            return completed
        outcome["status"] = "object_absent"
        outcome["issues"].append(
            f"nothing exists at {key}; if you deleted it yourself, record that with "
            "--confirm-deleted instead"
        )
        outcome["retry"] = (
            "Nothing to move; rerun the same command with --confirm-deleted to record your "
            "own deletion."
        )
        return outcome
    if stat.S_ISLNK(info.st_mode):
        outcome["status"] = "refused"
        outcome["issues"].append(
            f"{key} is a symlink; this command never follows or moves a symlink"
        )
        return outcome
    if stat.S_ISDIR(info.st_mode):
        outcome["status"] = "refused"
        outcome["issues"].append(f"{key} is a directory, not a retained file object")
        return outcome
    if not stat.S_ISREG(info.st_mode):
        outcome["status"] = "refused"
        outcome["issues"].append(f"{key} is not a regular file")
        return outcome
    if not in_scope:
        outcome["status"] = "out_of_scope"
        outcome["issues"].append(
            f"{key} lies outside the documented discovery scope {scope_dirs}; this command "
            "only moves objects inside it"
        )
        outcome["retry"] = (
            "Move the object back into the discovery scope, or record your own deletion "
            "with --confirm-deleted."
        )
        return outcome
    identity = _identity_of(info)
    if active_rows:
        row = active_rows[0]
        mismatches: List[str] = []
        if parsed is not None:
            if row.get("suffix") not in (None, "", "unknown") and row["suffix"] != parsed["suffix"]:
                mismatches.append(
                    f"the record names the suffix {row['suffix']} but the name says {parsed['suffix']}"
                )
            if row.get("original_path") and _key(row["original_path"]) != _key(
                source.parent / parsed["original"]
            ):
                mismatches.append(
                    f"the record names the original path {row['original_path']} but the name "
                    f"says {source.parent / parsed['original']}"
                )
            recorded_pid = row.get("writer_pid")
            if isinstance(recorded_pid, int) and recorded_pid != parsed["pid"]:
                mismatches.append(
                    f"the record names writer PID {recorded_pid} but the name says {parsed['pid']}"
                )
        if mismatches:
            outcome["status"] = "refused"
            outcome["issues"].extend(mismatches)
            outcome["issues"].append(
                "the object at this path is not the object the record names; refusing to move it"
            )
            outcome["retry"] = (
                "Rerun memory-retained-list and inspect this path before acting on it."
            )
            return outcome
        basis = "an active discovery record names this path"
        explained = [_row_digest(row) for row in active_rows]
    elif parsed is not None:
        basis = (
            "the private retained name rule matches inside the discovery scope and no "
            "active discovery record names it"
        )
        explained = []
    else:
        outcome["status"] = "refused"
        outcome["issues"].append(
            f"{key} carries neither the private retained name nor an active discovery "
            "record; it looks like an ordinary file, and this command never moves an "
            "ordinary file"
        )
        outcome["retry"] = (
            "Look up the retained path you meant with memory-retained-list."
        )
        return outcome
    for item in reversed(prior):
        recorded = item.get("object_identity")
        if (
            item.get("action") == "moved_to_trash"
            and isinstance(recorded, dict)
            and recorded.get("ino") == identity["ino"]
            and recorded.get("dev") == identity["dev"]
        ):
            outcome["ok"] = True
            outcome["status"] = "already_resolved"
            outcome["trash_path"] = item.get("trash_path")
            outcome["resolved_at"] = item.get("resolved_at")
            outcome["note"] = (
                "this exact object was already moved to the system trash; nothing was changed"
            )
            return outcome
    if not active_rows:
        # The only evidence for this object is its private name, and this path has
        # already been handled once.  Re-verify the actual identity against the
        # recorded one: if a different object now sits at this path, it is a
        # replacement, and an old record must never make this command move it.
        for item in reversed(prior):
            recorded = item.get("object_identity")
            if (
                item.get("action") in ("moved_to_trash", "confirmed_deleted")
                and isinstance(recorded, dict)
                and (recorded.get("dev") != identity["dev"] or recorded.get("ino") != identity["ino"])
            ):
                outcome["status"] = "object_replaced"
                outcome["issues"].append(
                    f"{key} was already handled, but the object at this path now is a "
                    f"different object than the recorded one (recorded dev/ino "
                    f"{recorded.get('dev')}/{recorded.get('ino')}, on disk "
                    f"{identity['dev']}/{identity['ino']}); this command will not move an "
                    "object it cannot match to a record"
                )
                outcome["retry"] = (
                    "Check this path in memory-retained-list: it is listed as pending with no "
                    "discovery record. Delete the object yourself, then record that with "
                    "--confirm-deleted."
                )
                return outcome
    # The identity a discovery record carries is the authority: it is the object the
    # user inspected and moved aside.  An identity given on the command line only
    # fills the gap when the record carries none, and it can never override a record.
    recorded_identity = active_rows[0].get("object_identity") if active_rows else None
    if isinstance(recorded_identity, dict) and recorded_identity.get("ino") is not None:
        expected = recorded_identity
        expected_source = "the discovery record that names this path"
    elif isinstance(expected_identity, dict) and expected_identity.get("ino") is not None:
        expected = expected_identity
        expected_source = "the identity given on the command line"
    else:
        expected = None
        expected_source = ""
    if active_rows and expected is None:
        outcome["status"] = "identity_unverified"
        outcome["issues"].append(
            f"{key} is named by a discovery record that carries no object identity, and no "
            "explicit identity was given on the command line; moving it now would move whatever "
            "happens to sit at this path, which this command will not call verified"
        )
        outcome["retry"] = (
            "Run memory-retained-list, read the dev/ino it reports for this path, then rerun the "
            "same command with --expect-dev <dev> --expect-ino <ino> naming exactly those numbers; "
            "if you deleted the object yourself, record that with --confirm-deleted instead."
        )
        return outcome
    if active_rows and expected is not None and not _identity_matches(expected, info):
        outcome["status"] = "object_replaced"
        outcome["issues"].append(
            f"{key} does not hold the object named by {expected_source} (recorded dev/ino "
            f"{expected.get('dev')}/{expected.get('ino')}, on disk "
            f"{identity['dev']}/{identity['ino']}); refusing to move an object that is not the "
            "recorded one"
        )
        outcome["retry"] = (
            "Rerun memory-retained-list and inspect this path before acting on it; if you deleted "
            "the recorded object yourself, record that with --confirm-deleted instead."
        )
        return outcome
    # An announcement that never became a move is retired before a new one is written.
    for live in _live_move_intents(intents or []):
        if _record_key(live.get("retained_path")) != key:
            continue
        if _identity_matches(live.get("object_identity"), info):
            cancel_move_intent(
                config,
                retained_path=key,
                digest=_row_digest(live),
                note=(
                    "the announced move did not happen: the object is still at its recorded path, "
                    "so the announcement is retired before a new one is written"
                ),
            )
    intent_entry = {
        "schema": INTENT_SCHEMA,
        "kind": "move_intent",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "retained_path": key,
        "original_path": _key(
            active_rows[0].get("original_path")
            if active_rows and active_rows[0].get("original_path")
            else (source.parent / (parsed["original"] if parsed else source.name))
        ),
        "suffix": (
            (active_rows[0].get("suffix") if active_rows else None)
            or (parsed["suffix"] if parsed else "unknown")
        ),
        "writer_pid": (
            (active_rows[0].get("writer_pid") if active_rows else None)
            or (parsed["pid"] if parsed else "unknown")
        ),
        "task_id": (active_rows[0].get("task_id") if active_rows else None) or "unknown",
        "operation": (active_rows[0].get("operation") if active_rows else None) or "unknown",
        "reason": (active_rows[0].get("reason") if active_rows else None) or "unknown",
        "object_identity": identity,
        "identity_source": expected_source or "the private retained name rule",
        "trash_dir": str(trash_dir(config)),
        "note": (
            "written before the move: if this command stops between moving the object and writing "
            "its resolution record, this line is what says a move of this object was started, and "
            "the next run of the same command completes the record from it"
        ),
    }
    intent_error = record_move_intent(config, intent_entry)
    if intent_error is not None:
        outcome["status"] = "not_recorded"
        outcome["issues"].append(intent_error)
        outcome["issues"].append(
            "the move was not started: this command never starts a move it cannot leave a trace of"
        )
        outcome["retry"] = (
            "Make the metadata directory writable, then run the same command again."
        )
        return outcome
    trash_path, error, conflict = _move_into_trash(config, source, info)
    if conflict is not None:
        # The announcement stays live on purpose: it is what keeps reporting the real
        # location of the object the record names.
        outcome["status"] = "move_conflict"
        outcome["conflict"] = conflict
        outcome["trash_path"] = conflict.get("recorded_object_preserved_at") or conflict.get(
            "captured_object_path"
        )
        outcome["issues"].append(conflict["detail"])
        if conflict.get("record_error"):
            if conflict.get("captured_object_kept_under_derived_name"):
                outcome["issues"].append(
                    f"the object captured by mistake at {conflict.get('captured_object_path')} "
                    f"could not be recorded: {conflict['record_error']}. It was left under a trash "
                    "name that carries this move's announcement and the object's own inode, so the "
                    "next run of memory-retained-list checks that name and reports exactly where "
                    "the object is; the user handles it by hand"
                )
            else:
                outcome["issues"].append(
                    f"the object captured by mistake at {conflict.get('captured_object_path')} "
                    f"could not be recorded: {conflict['record_error']}, and it could not be put on "
                    "a name carrying this move's announcement and the object's own inode either, so "
                    "it is kept exactly where this move left it and this message is the only note of "
                    "where it is kept; the user handles it by hand"
                )
        outcome["issues"].append(
            "the move announcement stays in place, so memory-retained-list keeps reporting where "
            "the object the record names really is; nothing was recorded as handled"
        )
        outcome["retry"] = (
            "Run memory-retained-list and read the announced move and the real locations it "
            "reports, then handle this path by hand; this command never records a move it did not "
            "carry out for the object the record names."
        )
        return outcome
    if error is not None:
        try:
            still_here = _same_object(info, os.lstat(str(source)))
        except OSError:
            still_here = False
        if still_here:
            # The announcement this run wrote, and any older announcement of the same
            # object, are retired: the move did not happen, and a stale announcement
            # must not be left for a later run to mistake for a move that did.
            stale = {_row_digest(intent_entry)}
            for live in _live_move_intents(intents or []):
                if _record_key(live.get("retained_path")) == key and _identity_matches(
                    live.get("object_identity"), info
                ):
                    stale.add(_row_digest(live))
            for digest in sorted(stale):
                cancel_move_intent(
                    config,
                    retained_path=key,
                    digest=digest,
                    note=f"the announced move did not happen: {error}",
                )
        outcome["status"] = "move_failed"
        outcome["issues"].append(error)
        outcome["issues"].append(
            f"the object is still at its original retained location {key}"
            if still_here
            else (
                f"the recorded name {key} is no longer in place; the locations reported above are "
                "where the objects really are"
            )
        )
        outcome["retry"] = (
            "Fix the reported cause and run the same command again; nothing was recorded as "
            "handled."
        )
        return outcome
    outcome["trash_path"] = str(trash_path)
    entry = _resolution_entry(
        key=key,
        action="moved_to_trash",
        rows=active_rows,
        object_identity=identity,
        trash_path=str(trash_path),
        confirmed_by_user=True,
        object_verified_absent=True,
        explained=explained,
        note=(
            f"moved into the system trash because {basis}; the same inode was preserved, so "
            "the object is recoverable from the trash, and no content was copied, adopted as "
            "an experience, or indexed"
        ),
    )
    error = record_resolution(config, entry)
    if error is not None:
        outcome["status"] = "moved_but_not_recorded"
        outcome["issues"].append(error)
        outcome["issues"].append(
            f"the object really is at {trash_path} now, but the resolution record could not "
            "be written, so the list still shows this path as pending"
        )
        outcome["retry"] = (
            "Make the metadata directory writable, then run the same command again to append "
            "the record."
        )
        return outcome
    outcome["ok"] = True
    outcome["status"] = "moved_to_trash"
    outcome["resolved_at"] = entry["resolved_at"]
    return outcome


def resolve_retained(
    config: Any,
    *,
    retained_path: Any,
    action: str,
    expected_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handle one retained object, serialized against every other process.

    ``_resolve_retained_locked`` below is the command itself.  What this wrapper adds is
    the one thing that command cannot do alone: reading "is this object already handled?",
    acting on the object and appending the record that answers that question are one
    critical section under an OS file lock.  Without it two processes racing for the same
    object both read "not handled yet" and both append a handling record -- the loser then
    reports a move it never made and the object is handled twice.  The object itself is
    protected separately by the exclusive directory-entry move; this lock protects the
    decision and its record and never replaces that move.

    A process that cannot take the lock within the bounded wait gets its own truthful
    answer (``move_busy``): nothing moved, nothing created, no record appended.  A
    process that cannot take the lock at all gets the same honesty (``lock_unavailable``):
    the command body is not run either, because a single-object change that is not
    serialized is exactly the double handling this lock exists to prevent, and the report
    names the real cause, the lock path and when to try again.
    """
    with _resolution_lock(config) as lock:
        if lock["status"] == "busy":
            return {
                "ok": False,
                "status": "move_busy",
                "action": action,
                "retained_path": str(retained_path),
                "trash_path": None,
                "record_path": str(resolutions_path(config)),
                "object_identity": None,
                "issues": [
                    "another process is handling a retained object right now; this run moved "
                    "nothing, created nothing and appended no handling record"
                ],
                "retry": "Run the same command again once the other process has finished.",
                "serialization": "busy",
                "note": (
                    "single-object user command: it handles only the object named on the command "
                    "line, never a batch, never a path it was not given, and it never deletes or "
                    "expires anything on its own"
                ),
            }
        if lock["status"] != "locked":
            return {
                "ok": False,
                "status": "lock_unavailable",
                "action": action,
                "retained_path": str(retained_path),
                "trash_path": None,
                "record_path": str(resolutions_path(config)),
                "object_identity": None,
                "issues": [
                    f"no cross-process lock could be taken, so this single-object command was "
                    f"not run: {lock['detail']}",
                    "this run moved nothing, created nothing and appended no handling record, "
                    "because a change that is not serialized could handle the same object twice",
                ],
                "retry": (
                    f"Fix the reported cause for the lock file {lock['lock_path']}, then run the "
                    "same command again."
                ),
                "serialization": "unavailable",
                "note": (
                    "single-object user command: it handles only the object named on the command "
                    "line, never a batch, never a path it was not given, and it never deletes or "
                    "expires anything on its own"
                ),
            }
        outcome = _resolve_retained_locked(
            config,
            retained_path=retained_path,
            action=action,
            expected_identity=expected_identity,
        )
        outcome["serialization"] = lock["status"]
        return outcome


def _resolve_retained_locked(
    config: Any,
    *,
    retained_path: Any,
    action: str,
    expected_identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handle exactly one retained object named on the command line.

    Two mutually exclusive actions, both run by the user in their own terminal:

    * ``trash`` -- move the object into the system trash.  The object must lie in
      the documented discovery scope and must be identified by an active discovery
      record (name fields re-verified) or by the private name rule.  The move is
      bound to the identity the record carries -- the object the user inspected --
      and ``expected_identity`` is used only when the record carries none; an object
      that does not match is refused and never moved.
    * ``confirm_deleted`` -- record that the user deleted the object themselves.
      The path must be named by a discovery record and the object must really be
      gone; nothing is ever deleted by this action.

    A move is announced in the move-intent record before it starts, so a move that
    succeeded while its resolution record could not be written is completed by the
    next run of the same command instead of being lost.

    Nothing here is a batch, nothing expires, and every failure reports the real
    location and what to do next instead of claiming success.
    """
    outcome: Dict[str, Any] = {
        "ok": False,
        "status": "refused",
        "action": action,
        "retained_path": "",
        "trash_path": None,
        "record_path": str(resolutions_path(config)),
        "object_identity": None,
        "issues": [],
        "retry": None,
        "note": (
            "single-object user command: it handles only the object named on the command "
            "line, never a batch, never a path it was not given, and it never deletes or "
            "expires anything on its own"
        ),
    }
    if action not in ACTIONS:
        outcome["issues"].append(
            f"unknown action {action!r}; this command has exactly two: --trash and "
            "--confirm-deleted"
        )
        return outcome
    if isinstance(expected_identity, dict) and (
        expected_identity.get("dev") is None or expected_identity.get("ino") is None
    ):
        outcome["issues"].append(
            "an expected identity needs both the device number and the inode number; one of "
            "--expect-dev/--expect-ino is missing, and half an identity is never used"
        )
        return outcome
    try:
        key = _record_key(retained_path)
    except OSError as exc:
        outcome["issues"].append(f"the named path cannot be resolved: {exc}")
        return outcome
    if not key or key == os.sep or key == str(Path.home()):
        outcome["issues"].append(f"{key} cannot be a retained object")
        return outcome
    outcome["retained_path"] = key
    source = Path(key)
    scope_dirs = [str(item) for item in retained_scan_dirs(config)]
    in_scope = _within_scope(key, scope_dirs)
    journal_entries, journal_problems = read_journal(config)
    resolutions, resolution_problems = read_resolutions(config)
    intents, intent_problems = read_move_intents(config)
    if journal_problems or resolution_problems or intent_problems:
        outcome["status"] = "records_unreadable"
        outcome["issues"].extend(journal_problems)
        outcome["issues"].extend(resolution_problems)
        outcome["issues"].extend(intent_problems)
        outcome["retry"] = (
            "Repair the discovery or resolution record, then run the same command again."
        )
        return outcome
    explained = _explained_digests(resolutions)
    rows_for_path = [
        row for row in journal_entries if _record_key(row.get("retained_path")) == key
    ]
    active_rows = [row for row in rows_for_path if _row_digest(row) not in explained]
    prior = [item for item in resolutions if _record_key(item.get("retained_path")) == key]
    parsed = parse_retained_name(source.name)
    try:
        info = os.lstat(str(source))
    except FileNotFoundError:
        info = None
    except OSError as exc:
        outcome["status"] = "unreadable"
        outcome["issues"].append(f"{key}: {exc}")
        outcome["retry"] = "Check the object's permissions, then run the same command again."
        return outcome
    if info is not None:
        outcome["object_identity"] = _identity_of(info)
    if action == "confirm_deleted":
        return _confirm_deleted(
            config,
            outcome,
            key,
            info,
            rows_for_path,
            prior,
            parsed is not None and in_scope,
            intents,
            resolutions,
        )
    return _move_to_trash(
        config,
        outcome,
        key,
        source,
        info,
        parsed,
        active_rows,
        prior,
        in_scope,
        scope_dirs,
        expected_identity,
        intents,
    )
