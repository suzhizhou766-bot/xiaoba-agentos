"""结构指纹定向测试：真实建库一次，用 sqlite_master 定义拦住表结构变化。

对应任务卡「三、结构指纹测试」。

做法：在临时目录里用真实存储类建一个全新数据库（不连线上库），读出
sqlite_master 中全部表、索引、触发器、视图的定义和 PRAGMA user_version，
做确定性排序与空白归一后算 SHA-256，与 tests/fixtures/schema_fingerprint.json
比对。

失败即表示表结构变了：必须同时升 DATABASE_SCHEMA_VERSION 并更新指纹文件，
这一版按迁移档走。
"""

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.store import EventStore

WORKSPACE = Path(__file__).resolve().parents[1]
FIXTURE_PATH = WORKSPACE / "tests" / "fixtures" / "schema_fingerprint.json"

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_sql(sql):
    """把 DDL 里的空白折叠成单个空格，去掉首尾空白。"""
    if sql is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(sql)).strip()


def read_schema_objects(db_path):
    """读出一致排序后的对象定义行与 user_version。"""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    objects = ["{}|{}|{}|{}".format(kind, name, table, _normalize_sql(sql))
               for (kind, name, table, sql) in rows]
    objects.sort()
    return objects, user_version


def compute_fingerprint(db_path):
    """返回 (sha256, 对象数量, user_version)。"""
    objects, user_version = read_schema_objects(db_path)
    payload = json.dumps({"objects": objects, "user_version": user_version},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest, len(objects), user_version


def build_fresh_database(root):
    """在 root 下用真实存储类初始化一个全新数据库，返回路径。"""
    db_path = Path(root) / "schema-fingerprint.sqlite3"
    EventStore(db_path).initialize()
    return db_path


def _load_fixture():
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


class SchemaFingerprintTest(unittest.TestCase):
    """表结构一旦变化，必须同时升版本号并更新指纹文件。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="schema-fingerprint-")
        cls.db_path = build_fresh_database(cls._tmp)
        cls.sha256, cls.objects, cls.user_version = compute_fingerprint(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_fixture_schema_version_matches_code_constant(self):
        fixture = _load_fixture()
        self.assertEqual(
            fixture.get("schema_version"), EventStore.DATABASE_SCHEMA_VERSION,
            "指纹文件里的 schema_version 必须等于代码里的 DATABASE_SCHEMA_VERSION")

    def test_database_user_version_matches_code_constant(self):
        self.assertEqual(
            self.user_version, EventStore.DATABASE_SCHEMA_VERSION,
            "PRAGMA user_version 必须等于代码里的 DATABASE_SCHEMA_VERSION")

    def test_fingerprint_matches_fixture(self):
        fixture = _load_fixture()
        if (fixture.get("sha256") != self.sha256
                or fixture.get("objects") != self.objects):
            self.fail(
                "表结构指纹与 tests/fixtures/schema_fingerprint.json 不一致：\n"
                "  指纹文件 schema_version={!r} sha256={!r} objects={!r}\n"
                "  真实建库 schema_version={!r} sha256={!r} objects={!r}\n"
                "改了表结构必须同时升 DATABASE_SCHEMA_VERSION"
                "（当前 {!r}）并更新指纹文件，这一版按迁移档走。".format(
                    fixture.get("schema_version"), fixture.get("sha256"),
                    fixture.get("objects"),
                    EventStore.DATABASE_SCHEMA_VERSION, self.sha256, self.objects,
                    EventStore.DATABASE_SCHEMA_VERSION))

    def test_added_column_changes_fingerprint(self):
        """给任意一张表加一列，指纹必须变（否则这个测试拦不住表结构改动）。"""
        copy_dir = tempfile.mkdtemp(prefix="schema-fingerprint-copy-")
        try:
            target = Path(copy_dir) / "mutated.sqlite3"
            shutil.copyfile(str(self.db_path), str(target))
            table = self._first_user_table(target)
            conn = sqlite3.connect(str(target))
            try:
                conn.execute(
                    'ALTER TABLE "{}" ADD COLUMN "_fingerprint_probe" TEXT'.format(table))
                conn.commit()
            finally:
                conn.close()
            mutated, _, _ = compute_fingerprint(target)
            self.assertNotEqual(self.sha256, mutated,
                                "加列后指纹没有变，结构指纹测试失效")
        finally:
            shutil.rmtree(copy_dir, ignore_errors=True)

    def test_fingerprint_is_path_independent(self):
        """换个目录再建一次库，指纹必须一致（避免把临时路径算进去）。"""
        other_dir = tempfile.mkdtemp(prefix="schema-fingerprint-other-")
        try:
            other_db = build_fresh_database(other_dir)
            other, other_objects, other_version = compute_fingerprint(other_db)
            self.assertEqual(self.sha256, other, "同一份代码在不同目录建库指纹不一致")
            self.assertEqual(self.objects, other_objects)
            self.assertEqual(self.user_version, other_version)
        finally:
            shutil.rmtree(other_dir, ignore_errors=True)

    def test_vacuum_keeps_fingerprint(self):
        """只重写库文件、不动结构时指纹不变，避免误报。"""
        copy_dir = tempfile.mkdtemp(prefix="schema-fingerprint-vacuum-")
        try:
            target = Path(copy_dir) / "vacuum.sqlite3"
            shutil.copyfile(str(self.db_path), str(target))
            conn = sqlite3.connect(str(target))
            try:
                conn.execute("VACUUM")
                conn.commit()
            finally:
                conn.close()
            rebuilt, rebuilt_objects, _ = compute_fingerprint(target)
            self.assertEqual(self.sha256, rebuilt, "只重写库文件不应改变结构指纹")
            self.assertEqual(self.objects, rebuilt_objects)
        finally:
            shutil.rmtree(copy_dir, ignore_errors=True)

    @staticmethod
    def _first_user_table(db_path):
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        finally:
            conn.close()
        if not rows:
            raise AssertionError("临时库里没有任何用户表")
        return rows[0][0]


if __name__ == "__main__":
    unittest.main()
