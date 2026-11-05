"""决策库（repository）/决策分类（catalogue）对照表导入 → bom_catalog.sqlite。

1) repositories 表：源 repositoryId ↔ 库名 对应表（列分隔符不定：Tab / 逗号 /
   空白均容忍；列顺序不定：按 16~64 位十六进制 id 形态自动识别哪列是 id，
   其余列取第一个非 id 列作名称；表头/注释行自动跳过）。
2) catalogues 表：源决策分类对照表，每行
   `分类名称(含路径) \t repositoryId \t catalogueId`，一个库可对应多个分类，
   前端按 repositoryId 检索可选 catalogueId 列表。两个 id 列时按列序：
   倒数第二个 = repositoryId、最后一个 = catalogueId，其余非 id 列拼作名称。

用法（独立刷新，不必重跑全量 ETL）：
    python -m rule_editor.etl.repositories
    python -m rule_editor.etl.repositories --file 新车险915/repository.txt --db data/bom_catalog.sqlite
    python -m rule_editor.etl.repositories --cata-file 新车险915/respository_cata.txt

schema:
    repositories(id TEXT PRIMARY KEY, name TEXT)
    catalogues(repo_id TEXT, cat_id TEXT, name TEXT, PRIMARY KEY(repo_id, cat_id))
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from ..config import load_config, get, project_root  # noqa: E402

# repositoryId 形态：十六进制串（32 位 uuid 无连字符，放宽到 16~64 位十六进制）
_HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")
# 明显是表头的 cell
_HEADER_RE = re.compile(r"^(id|uuid|repository_?id|repository|name|库名|库名称|"
                        r"决策库|决策库id|决策库ID|名称)$", re.IGNORECASE)


def _split_cols(line: str) -> list[str]:
    """行切列：Tab > 逗号 > 连续空白（引号字段感知，"安徽,xx" 不被逗号截断）。"""
    if "\t" in line:
        delim = "\t"
    elif "," in line:
        delim = ","
    else:
        return [c.strip().strip('"').strip("'")
                for c in re.split(r"\s{2,}|\s+", line.strip()) if c and c.strip()]
    try:
        cols = next(csv.reader([line], delimiter=delim))
    except Exception:
        cols = line.split(delim)
    return [c.strip() for c in cols if c and c.strip()]


def _read_text(path: str) -> str:
    """读对照表文件文本；不存在/为空返回 ""。编码尝试 utf-8-sig（BOM）→ gbk → utf-8。"""
    if not path or not os.path.exists(path):
        return ""
    raw = b""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    if not raw.strip():
        return ""
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_repository_file(path: str) -> list[dict]:
    """解析 repository 对照表文件 → [{id, name}]（按 id 去重，保序）。

    文件不存在/为空返回 []。
    """
    text = _read_text(path)
    if not text:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = _split_cols(line)
        if not cols:
            continue
        rid = ""
        name = ""
        id_idx = -1
        for i, c in enumerate(cols):
            if _HEX_ID_RE.match(c):
                rid, id_idx = c.lower(), i
                break
        if not rid:
            # 无十六进制形态列：退化为约定 第 1 列=id、第 2 列=名称
            rid = cols[0].lower()
            id_idx = 0
            if _HEADER_RE.match(rid) or len(cols) < 2:
                continue  # 表头行或残缺行
            name = cols[1]
        else:
            for i, c in enumerate(cols):
                if i != id_idx and not _HEX_ID_RE.match(c):
                    name = c
                    break
        if not rid or (name and _HEADER_RE.match(name)):
            continue  # 表头行（id 列恰好也长得像 hex 时按名称列判）
        if rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "name": name or rid})
    return out


def build_repositories(src_path: str, bom_db: str) -> int:
    """解析 src_path 写入 bom_db 的 repositories 表（drop-recreate），返回行数。"""
    rows = parse_repository_file(src_path)
    conn = sqlite3.connect(bom_db)
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS repositories")
        cur.execute("CREATE TABLE repositories(id TEXT PRIMARY KEY, name TEXT)")
        cur.executemany("INSERT OR IGNORE INTO repositories(id, name) VALUES(?, ?)",
                        [(r["id"], r["name"]) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def parse_catalogue_file(path: str) -> list[dict]:
    """解析决策分类对照表 → [{repo_id, cat_id, name}]（按 (repo_id,cat_id) 去重，保序）。

    每行 `分类名称(含路径) \\t repositoryId \\t catalogueId`：按 16~64 位十六进制
    形态识别 id 列，两个 id 列时倒数第二个=repositoryId、最后一个=catalogueId，
    其余列拼作名称；不足两个 id 列（表头/残缺行）跳过。
    """
    text = _read_text(path)
    if not text:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = _split_cols(line)
        if len(cols) < 2:
            continue
        hex_idx = [i for i, c in enumerate(cols) if _HEX_ID_RE.match(c)]
        if len(hex_idx) < 2:
            continue
        repo_i, cat_i = hex_idx[-2], hex_idx[-1]
        repo, cat = cols[repo_i].lower(), cols[cat_i].lower()
        name = " ".join(c for i, c in enumerate(cols)
                        if i not in (repo_i, cat_i)).strip()
        if _HEADER_RE.match(name):
            name = ""
        key = (repo, cat)
        if key in seen:
            continue
        seen.add(key)
        out.append({"repo_id": repo, "cat_id": cat, "name": name or cat})
    return out


def build_catalogues(src_path: str, bom_db: str) -> int:
    """解析 src_path 写入 bom_db 的 catalogues 表（drop-recreate），返回行数。"""
    rows = parse_catalogue_file(src_path)
    conn = sqlite3.connect(bom_db)
    try:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS catalogues")
        cur.execute("CREATE TABLE catalogues(repo_id TEXT, cat_id TEXT, name TEXT,"
                    " PRIMARY KEY(repo_id, cat_id))")
        cur.executemany("INSERT OR IGNORE INTO catalogues(repo_id, cat_id, name)"
                        " VALUES(?, ?, ?)",
                        [(r["repo_id"], r["cat_id"], r["name"]) for r in rows])
        cur.execute("CREATE INDEX IF NOT EXISTS idx_catalogues_repo"
                    " ON catalogues(repo_id)")
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="导入决策库/决策分类对照表到 bom_catalog.sqlite")
    ap.add_argument("--file", default="", help="repository 对照表文件路径（默认取 config data.repository_file）")
    ap.add_argument("--cata-file", default="", help="catalogue 对照表文件路径（默认取 config data.catalogue_file）")
    ap.add_argument("--db", default="", help="目标 sqlite（默认取 config etl.bom_db）")
    args = ap.parse_args()

    load_config()
    root = project_root()
    db = args.db or get("etl", "bom_db", "data/bom_catalog.sqlite")
    if not os.path.isabs(db):
        db = os.path.join(root, db)

    def _abs(p: str) -> str:
        return p if (not p or os.path.isabs(p)) else os.path.join(root, p)

    src = _abs(args.file or get("data", "repository_file", "") or "")
    if src and os.path.exists(src):
        n = build_repositories(src, db)
        print(f"[repositories] {src} -> {db}：{n} 条")
    else:
        print(f"[repositories] 数据源不存在：{src or '(config 未配置 data.repository_file)'}")

    csrc = _abs(args.cata_file or get("data", "catalogue_file", "") or "")
    if csrc and os.path.exists(csrc):
        n = build_catalogues(csrc, db)
        print(f"[catalogues] {csrc} -> {db}：{n} 条")
    else:
        print(f"[catalogues] 数据源不存在：{csrc or '(config 未配置 data.catalogue_file)'}")


if __name__ == "__main__":
    main()
