#!/usr/bin/env python3
"""Query the CUR (Cost and Usage Report, "Data Export" CUR 2.0 parquet) with DuckDB. Read-only.

    pip install -r requirements.txt            # duckdb
    python3 cur_query.py 2026-09 top                                  # top 25 resources by cost this month
    python3 cur_query.py 2026-09 service AmazonS3                     # one service: resource x usage type
    python3 cur_query.py 2026-09 daily AmazonS3 my-bucket-name        # one resource by day
    python3 cur_query.py 2026-09 sql "select ... from cur ..."        # anything else

The export's S3 path comes from ../config.json: "cur_s3_path": "s3://bucket/prefix/data".
The export must include resource IDs (line_item_resource_id), parquet format, hourly granularity.

Downloads every parquet part of the month once into ./cur_cache/<month>/ and exposes it as the view `cur`.
Re-syncs before each query, because AWS rewrites the month's files on each delivery.
"""
import json
import os
import subprocess
import sys

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))


def config():
    path = os.path.join(HERE, "..", "config.json")
    cfg = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    if not cfg.get("cur_s3_path"):
        sys.exit("config.json has no cur_s3_path. Set up a CUR 2.0 data export with resource IDs first "
                 "(README section 2), then put its s3://bucket/prefix/data path in config.json.")
    return cfg


def load(month):
    cfg = config()
    cache = os.path.join(os.getcwd(), "cur_cache", month)
    os.makedirs(cache, exist_ok=True)
    src = f"{cfg['cur_s3_path'].rstrip('/')}/BILLING_PERIOD={month}/"
    print(f"syncing {src} -> {cache}", flush=True)
    cmd = ["aws", "s3", "sync", src, cache, "--exclude", "*", "--include", "*.parquet", "--only-show-errors"]
    if cfg.get("profile"):
        cmd += ["--profile", cfg["profile"]]
    subprocess.run(cmd, check=True)
    parts = sorted(f for f in os.listdir(cache) if f.endswith(".parquet"))
    if not parts:
        sys.exit(f"no parquet for {month}")
    print(f"{len(parts)} part(s): {', '.join(parts)}", flush=True)
    con = duckdb.connect()
    con.execute(f"create view cur as select * from read_parquet('{cache}/*.parquet', union_by_name=true)")
    return con


def show(con, sql):
    res = con.execute(sql)
    cols = [d[0] for d in res.description]
    rows = res.fetchall()
    widths = [max(len(str(c)), *(len(f"{r[i]:.2f}" if isinstance(r[i], float) else str(r[i])) for r in rows))
              if rows else len(c) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join((f"{v:.2f}" if isinstance(v, float) else str(v)).ljust(w) for v, w in zip(r, widths)))


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    month, mode, *args = sys.argv[1:]
    con = load(month)
    if mode == "top":
        show(con, """select line_item_product_code AS svc, line_item_resource_id AS resource,
                     round(sum(line_item_unblended_cost),2) AS cost
                     from cur where line_item_line_item_type='Usage'
                     group by 1,2 order by cost desc limit 25""")
    elif mode == "service":
        show(con, f"""select line_item_resource_id AS resource, line_item_usage_type AS usage_type,
                      round(sum(line_item_usage_amount),2) AS qty, round(sum(line_item_unblended_cost),2) AS cost
                      from cur where line_item_product_code='{args[0]}' and line_item_line_item_type='Usage'
                      group by 1,2 order by cost desc limit 40""")
    elif mode == "daily":
        show(con, f"""select cast(line_item_usage_start_date as date) AS d, line_item_usage_type AS usage_type,
                      round(sum(line_item_usage_amount),3) AS qty, round(sum(line_item_unblended_cost),3) AS cost
                      from cur where line_item_product_code='{args[0]}' and line_item_resource_id='{args[1]}'
                      group by 1,2 order by 1,4 desc""")
    elif mode == "sql":
        show(con, args[0])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
