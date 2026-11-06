"""任务卡台账：扫描任务卡目录、读写发卡台账、自动发现回执文件。

监看中心的“任务卡”页和宿主 Agent 用的 tools/taskcard.py 共用这一份逻辑。

- 任务卡就是任务卡目录里的 Markdown 文件（文件名以“卡N-”开头，第一行“# …任务卡 N：标题”）。
- 发卡台账是一个 JSONL 文件，每行一个事件：发卡、回执、退回、核对通过、完成、备注。
  只追加、不改写；状态取每张卡最新的事件。
- 没登记过的卡按卡里的“开始条件”推断：写着“现在可发”的算待发，写着“已完成”的算历史卡，其它算等待条件。
- 工作区里文件名带“回执”和“卡N”的 .md/.txt 文件当作自动发现的回执，比最近一次发卡更新就提示待核对。
"""
import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

EVENTS = {
    "issue": "已发卡",
    "receipt": "回执待核对",
    "returned": "退回返工",
    "verified": "已核对",
    "done": "已完成",
    "note": None,          # 备注不改变状态
}
STATES = ("ready", "waiting", "issued", "receipt", "returned", "verified", "done", "history")
STATE_TEXT = {
    "ready": "待发", "waiting": "等待条件", "issued": "已发卡", "receipt": "回执待核对",
    "returned": "退回返工", "verified": "已核对", "done": "已完成", "history": "已完成（历史）",
}
EVENT_STATE = {"issue": "issued", "receipt": "receipt", "returned": "returned", "verified": "verified", "done": "done"}

CARD_FILE_RE = re.compile(r"^卡(\d+)-.+\.md$")
TITLE_RE = re.compile(r"^#\s*(.*?任务卡\s*(\d+)\s*[：:]\s*(.+?))\s*$")
VERSION_RE = re.compile(r"v\d+\.\d+\.\d+")
RECEIPT_CARD_RE = re.compile(r"卡\s*(\d+)(?!\d)")
RECEIPT_SUFFIXES = (".md", ".txt")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".runtime", "_待删"}
MAX_TEXT = 256 * 1024
MAX_NOTE = 2000
MAX_LEDGER = 8 * 1024 * 1024


def _field(lines, name):
    prefix = name + "："
    for line in lines[:12]:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def read_card(path):
    """解析一张卡的头部。认不出来（不是任务卡格式）返回 None。"""
    match = CARD_FILE_RE.match(path.name)
    if not match:
        return None
    try:
        stat = path.stat()
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            head = [handle.readline().rstrip("\n") for _ in range(12)]
    except OSError:
        return None
    title_match = TITLE_RE.match(head[0] if head else "")
    number = int(match.group(1))
    title = title_match.group(3) if title_match else path.stem.split("-", 1)[-1]
    if title_match and int(title_match.group(2)) != number:
        title = path.stem.split("-", 1)[-1]
    version = VERSION_RE.search(title) or VERSION_RE.search(path.name)
    return {
        "id": number,
        "title": title,
        "version": version.group(0) if version else "",
        "start": _field(head, "开始条件"),
        "who": _field(head, "适合谁接"),
        "path": str(path),
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }


def inferred_state(start):
    """只有写明“现在可发”才算待发；“…合并后可发”这类带条件的算等待条件。"""
    if "现在可发" in start:
        return "ready"
    if "已完成" in start:
        return "history"
    return "waiting"


class Ledger:
    """发卡台账（JSONL，只追加）。"""

    def __init__(self, path):
        self.path = Path(path)

    def events(self):
        if not self.path.is_file():
            return []
        if self.path.stat().st_size > MAX_LEDGER:
            raise ValueError("发卡台账超过 8 MB，请先归档旧记录")
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    out.append({"bad_line": number})
                    continue
                if isinstance(item, dict) and item.get("event") in EVENTS and isinstance(item.get("card"), int):
                    out.append(item)
                else:
                    out.append({"bad_line": number})
        return out

    def append(self, card, event, *, by="", to="", note="", path="", now=None):
        if event not in EVENTS:
            raise ValueError("未知事件：%s（可用：%s）" % (event, "、".join(EVENTS)))
        if not isinstance(card, int) or card <= 0:
            raise ValueError("卡号必须是正整数")
        note = str(note or "").strip()
        if len(note) > MAX_NOTE:
            raise ValueError("备注不能超过 %d 字" % MAX_NOTE)
        record = {"at": round(now if now is not None else time.time(), 3), "card": card, "event": event}
        for key, value in (("by", by), ("to", to), ("note", note)):
            value = str(value or "").strip()
            if value:
                record[key] = value[:MAX_NOTE]
        if path:
            target = Path(path).expanduser().resolve()
            if not target.is_file():
                raise ValueError("回执文件不存在：%s" % target)
            record["path"] = str(target)
            record["sha256"] = _sha(target)
        if event == "receipt" and not path and not note:
            raise ValueError("登记回执要给 --path（回执文件）或 --note（回执贴在对话里时写摘要）")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return record


def scan_receipts(root, depth=3):
    """在 root 下（不超过 depth 层）找文件名带“回执”和“卡N”的 .md/.txt 文件，返回 {卡号: [文件]}。"""
    found = {}
    root = Path(root)
    if not root.is_dir():
        return found

    def walk(folder, level):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            name = entry.name
            if name.startswith(".") and name not in (".",):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if level < depth and name not in SKIP_DIRS:
                        walk(entry.path, level + 1)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if "回执" not in name or not name.endswith(RECEIPT_SUFFIXES):
                continue
            for number in {int(m.group(1)) for m in RECEIPT_CARD_RE.finditer(name)}:
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                found.setdefault(number, []).append({"path": entry.path, "mtime": mtime, "name": name})

    walk(str(root), 1)
    for items in found.values():
        items.sort(key=lambda item: item["mtime"])
    return found


class Board:
    """把卡、台账、自动发现的回执合成页面要的数据。"""

    def __init__(self, card_dirs, ledger_path, receipt_root="", receipt_depth=3):
        self.card_dirs = [Path(item) for item in card_dirs]
        self.ledger = Ledger(ledger_path)
        self.receipt_root = Path(receipt_root) if receipt_root else None
        self.receipt_depth = int(receipt_depth)

    def cards(self):
        cards = {}
        for folder in self.card_dirs:
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                card = read_card(path)
                if card and card["id"] not in cards:
                    card["dir"] = str(folder)
                    cards[card["id"]] = card
        return cards

    def find_card(self, number):
        return self.cards().get(number)

    def snapshot(self):
        cards = self.cards()
        warnings = []
        try:
            events = self.ledger.events()
        except (OSError, ValueError) as exc:
            events, warnings = [], ["发卡台账读不了：%s" % exc]
        bad = [item["bad_line"] for item in events if "bad_line" in item]
        if bad:
            warnings.append("发卡台账有 %d 行格式不对（第 %s 行），已跳过" % (len(bad), "、".join(map(str, bad[:5]))))
        events = [item for item in events if "bad_line" not in item]
        receipts = scan_receipts(self.receipt_root, self.receipt_depth) if self.receipt_root else {}

        by_card = {}
        for item in events:
            by_card.setdefault(item["card"], []).append(item)
        unknown = sorted(set(by_card) - set(cards))
        if unknown:
            warnings.append("台账里登记了找不到卡文件的卡：%s" % "、".join("卡 %d" % n for n in unknown))

        rows = []
        for number, card in cards.items():
            history = sorted(by_card.get(number, []), key=lambda item: item["at"])
            state = inferred_state(card["start"])
            source = "inferred"
            issued_at = to = None
            last_check = 0.0
            for item in history:
                if item["event"] in EVENT_STATE:
                    state, source = EVENT_STATE[item["event"]], "ledger"
                if item["event"] == "issue":
                    issued_at, to = item["at"], item.get("to") or to
                if item["event"] in ("verified", "returned", "done", "receipt"):
                    last_check = max(last_check, item["at"])
            registered = {item.get("path") for item in history if item.get("path")}
            found = [dict(item, registered=item["path"] in registered) for item in receipts.get(number, [])]
            # 自动发现的回执比最近一次发卡、核对都新：提示待核对
            fresh = [item for item in found if not item["registered"] and issued_at and item["mtime"] > max(issued_at, last_check)]
            if fresh and state in ("issued", "returned"):
                state, source = "receipt", "found"
            receipt_events = [item for item in history if item["event"] == "receipt"]
            latest_receipt = max([item["at"] for item in receipt_events] + [item["mtime"] for item in found] or [0]) or None
            rows.append(dict(card, state=state, state_text=STATE_TEXT[state], state_source=source,
                             issued_at=issued_at, to=to, events=history, receipts_found=found,
                             receipt_count=len(receipt_events) + sum(1 for item in found if not item["registered"]),
                             latest_receipt=latest_receipt,
                             updated=max([card["mtime"]] + [item["at"] for item in history] + [item["mtime"] for item in found])))
        rows.sort(key=lambda row: -row["id"])
        counts = {state: 0 for state in STATES}
        for row in rows:
            counts[row["state"]] += 1
        return {"ok": True, "cards": rows, "counts": counts, "warnings": warnings,
                "card_dirs": [str(item) for item in self.card_dirs], "ledger": str(self.ledger.path),
                "generated_at": time.time()}

    def readable(self, path):
        """页面只能读任务卡目录里的卡、台账里登记的回执和自动发现的回执。"""
        try:
            target = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not target.is_file():
            return None
        for folder in self.card_dirs:
            try:
                if target.parent == folder.resolve() and CARD_FILE_RE.match(target.name):
                    return target
            except OSError:
                continue
        snapshot = self.snapshot()
        allowed = set()
        for row in snapshot["cards"]:
            allowed.update(item.get("path") for item in row["events"] if item.get("path"))
            allowed.update(item["path"] for item in row["receipts_found"])
        allowed = {str(Path(item).resolve()) for item in allowed if item}
        return target if str(target) in allowed else None

    def read_text(self, path):
        target = self.readable(path)
        if target is None:
            raise PermissionError("只能查看任务卡目录里的卡和已登记或自动发现的回执")
        size = target.stat().st_size
        with open(target, "rb") as handle:
            data = handle.read(MAX_TEXT)
        return {"ok": True, "path": str(target), "size": size, "truncated": size > MAX_TEXT,
                "sha256": _sha(target), "text": data.decode("utf-8", errors="replace")}
