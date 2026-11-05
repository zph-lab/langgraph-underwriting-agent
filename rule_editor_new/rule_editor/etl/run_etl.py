"""ETL 主入口。

用法：
    python -m rule_editor.etl.run_etl

执行：
1. bom_catalog.build_from_bom —— BOM 目录（对象/字段/运算符兼容表）
2. value_dict.build_value_dict —— 值词典 + 字段运算符频次
3. freq_stats.build_freq_stats —— 状态机条件频次
4. 将语料实际用过的运算符频次回填 bom_catalog.field_operators.count
"""
from __future__ import annotations

import os
import sys
import time

# 保证以项目根为工作目录时可直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from ..config import load_config, get, project_root  # noqa: E402
from .bom_catalog import BomCatalog  # noqa: E402
from .value_dict import build_value_dict, load_field_ops_count  # noqa: E402
from .freq_stats import build_freq_stats  # noqa: E402
from .corpus_index import build_corpus_index  # noqa: E402
from .repositories import build_repositories, build_catalogues  # noqa: E402


def main() -> None:
    load_config()
    root = project_root()
    samples_dir = get("data", "samples_dir")
    bom_path = get("data", "bom_path")
    bom_db = get("etl", "bom_db")
    value_db = get("etl", "value_dict_db")
    freq_db = get("etl", "freq_stats_db")
    corpus_db = get("etl", "corpus_db")

    t0 = time.time()
    print(f"[ETL] bom_path      = {bom_path}")
    print(f"[ETL] samples_dir   = {samples_dir}")
    print(f"[ETL] bom_db        = {bom_db}")

    # 1. BOM 目录
    cat = BomCatalog(bom_db)
    n = cat.build_from_bom(bom_path)
    print(f"[ETL] BOM catalog built: {n} nodes  ({time.time()-t0:.1f}s)")
    cat.close()

    # 2. 值词典 + 字段运算符频次
    stat = build_value_dict(samples_dir, value_db)
    print(f"[ETL] value dict: {stat}  ({time.time()-t0:.1f}s)")

    # 3. 状态机频次
    fs = build_freq_stats(samples_dir, freq_db)
    print(f"[ETL] freq stats: {fs}  ({time.time()-t0:.1f}s)")

    # 3b. 规则语料索引（相似规则引用 / few-shot）
    cs = build_corpus_index(samples_dir, corpus_db)
    print(f"[ETL] corpus index: {cs}  ({time.time()-t0:.1f}s)")

    # 4. 回填运算符频次到 bom_catalog.field_operators.count（按 field_path 匹配）
    ops = load_field_ops_count(value_db)
    cat = BomCatalog(bom_db)
    cat.connect()
    cur = cat._conn.cursor()
    cnt = 0
    # 若该字段不在 bom 中（field_operators 无对应行），则补插
    for fp, ops_map in ops.items():
        for op, c in ops_map.items():
            cur.execute(
                "UPDATE field_operators SET count=? "
                "WHERE field_path=? AND operator_code=?",
                (c, fp, op))
            if cur.rowcount == 0:
                # 字段可能未出现在 bom（极少数），尝试补插（需 field_id）
                row = cat._conn.execute(
                    "SELECT field_id FROM fields WHERE field_path=?", (fp,)).fetchone()
                if row:
                    cur.execute(
                        "INSERT OR IGNORE INTO field_operators"
                        "(field_id,field_path,operator_code,operator_label,count)"
                        " VALUES(?,?,?,?,?)",
                        (row[0], fp, op, op, c))
            cnt += 1
    cat._conn.commit()
    print(f"[ETL] backfilled operator counts: {cnt} rows")
    cat.close()

    # 5. 决策库 repositoryId↔库名 对照表（可选：文件缺失/为空则跳过，不影响主流程）
    repo_file = get("data", "repository_file") or ""
    if repo_file:
        rp = repo_file if os.path.isabs(repo_file) else os.path.join(root, repo_file)
        n_repo = build_repositories(rp, bom_db)
        print(f"[ETL] repositories: {n_repo} rows -> {bom_db}（源 {rp}）")
    else:
        print("[ETL] repositories: 未配置 data.repository_file，跳过")

    # 5.5 决策分类 repositoryId→catalogueId 对照表（可选，同上）
    cata_file = get("data", "catalogue_file") or ""
    if cata_file:
        cp = cata_file if os.path.isabs(cata_file) else os.path.join(root, cata_file)
        n_cat = build_catalogues(cp, bom_db)
        print(f"[ETL] catalogues: {n_cat} rows -> {bom_db}（源 {cp}）")
    else:
        print("[ETL] catalogues: 未配置 data.catalogue_file，跳过")

    print(f"[ETL] done in {time.time()-t0:.1f}s")
    print(f"[ETL] outputs:")
    print(f"      {bom_db}")
    print(f"      {value_db}")
    print(f"      {freq_db}")
    print(f"      {corpus_db}")


if __name__ == "__main__":
    main()
