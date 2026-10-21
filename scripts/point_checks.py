#!/usr/bin/env python3
"""Read-only AWS cost point checks.

Every call here is a Describe/Get/List. Nothing is written to AWS.
Cost of one run: ~10-20 Cost Explorer requests ($0.01 each) + free CloudWatch/EC2 reads.

    python3 -u point_checks.py > audit.log 2>&1      # then read audit.log
    python3 point_checks.py anomalies logs           # only some sections
    python3 point_checks.py selftest                 # date logic self-check, no AWS calls

Settings come from ../config.json (see ../config.example.json). Missing keys use the defaults below.
Prints a status line before each check so a slow call is never silent.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "profile": "",
    "region": "",
    "tax_rate": 0.0,
    # Flat-rate lines that bill the same amount every day (e.g. {"Amazon ElastiCache": 3.60}).
    # Optional second tell for an unsettled day, on top of the D+1 12:00 UTC time rule.
    "settle_tells": {},
    "s3_buckets_to_watch": [],
    "cur_s3_path": "",
    "price_ebs_gp3_gb_month": 0.08,
    "price_logs_ingest_gb": 0.50,
}


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "..", "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
TAX = float(CFG["tax_rate"])
TODAY = dt.date.today()
NOW_UTC = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
T0 = time.time()


def aws(*args):
    cmd = ["aws", *args, "--output", "json"]
    if CFG["profile"]:
        cmd += ["--profile", CFG["profile"]]
    if CFG["region"]:
        cmd += ["--region", CFG["region"]]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "error"
        print(f"  aws {args[0]} {args[1]}: {err[:200]}", flush=True)
        return {"_error": err}
    return json.loads(r.stdout) if r.stdout.strip() else {}


def phase(name):
    print(f"\n## {name}   [{time.time() - T0:.0f}s]", flush=True)


def same_date_last_month(day):
    """Oct 5 -> Sep 5. Free tiers reset on the 1st, so only the same dates compare across months.
    None when last month has no such date (Mar 30 -> no Feb 30)."""
    prev_last = day.replace(day=1) - dt.timedelta(days=1)
    return prev_last.replace(day=day.day) if day.day <= prev_last.day else None


def reference_day(day):
    """The day to compare `day` with: a week earlier if that is in the same month and past day 5,
    else the same date last month. Never a month's start against the previous month's end."""
    week = day - dt.timedelta(days=7)
    return week if week.month == day.month and week.day > 5 else same_date_last_month(day)


def settled_by_time(day, now=None):
    """A Cost Explorer day is final after D+1 12:00 UTC."""
    now = now or NOW_UTC
    return now >= dt.datetime.combine(day + dt.timedelta(days=1), dt.time(12))


def unsettled_tells(row):
    """Flat-rate lines reading below their daily amount: the day is still posting."""
    out = []
    for svc, base in CFG["settle_tells"].items():
        v = row.get(svc)
        if v is not None and v < float(base) * 0.97:
            out.append(f"{svc.split()[-1]} {v:.2f}/{float(base):.2f}")
    return out


def settled_days(daily):
    return [d for d in sorted(daily)
            if settled_by_time(dt.date.fromisoformat(d)) and not unsettled_tells(daily[d])]


def usage_filter():
    return json.dumps({"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}})


def ce_daily_by_service(start, end):
    d = aws("ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}",
            "--granularity", "DAILY", "--metrics", "UnblendedCost",
            "--filter", usage_filter(), "--group-by", "Type=DIMENSION,Key=SERVICE")
    out = {}
    for r in d.get("ResultsByTime", []):
        day = r["TimePeriod"]["Start"]
        out[day] = {g["Keys"][0]: float(g["Metrics"]["UnblendedCost"]["Amount"]) for g in r["Groups"]}
    return out


def check_months():
    phase("1. Monthly bill by record type (Usage vs Tax)")
    start = (TODAY.replace(day=1) - dt.timedelta(days=100)).replace(day=1)
    d = aws("ce", "get-cost-and-usage", "--time-period", f"Start={start},End={TODAY}",
            "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
            "--group-by", "Type=DIMENSION,Key=RECORD_TYPE")
    if "_error" in d:
        return
    print(f"  {'month':8} {'usage':>10} {'tax':>9} {'other':>8} {'total':>10}  tax%")
    for r in d["ResultsByTime"]:
        g = {x["Keys"][0]: float(x["Metrics"]["UnblendedCost"]["Amount"]) for x in r["Groups"]}
        u, t = g.pop("Usage", 0), g.pop("Tax", 0)
        o = sum(g.values())
        pct = f"{t / u * 100:5.1f}" if u else "  -"
        print(f"  {r['TimePeriod']['Start'][:7]:8} {u:10.2f} {t:9.2f} {o:8.2f} {u + t + o:10.2f}  {pct}")
    print("  note: tax is stamped on day 1 and revised all month; the current month's tax is partial.")


def check_daily(daily):
    phase("2. Daily usage, last 15 days, vs a week earlier (same date last month across a month boundary)")
    days = sorted(daily)
    tot = {d: sum(daily[d].values()) for d in days}
    for d in days[-15:]:
        wd = dt.date.fromisoformat(d)
        ref = reference_day(wd)
        prev = ref.isoformat() if ref else ""
        delta = f"{tot[d] - tot[prev]:+7.2f} vs {prev[5:]} {ref.strftime('%a')}" if prev in tot else ""
        why = []
        if not settled_by_time(wd):
            why.append("before D+1 12:00 UTC")
        why += unsettled_tells(daily[d])
        flag = f"  NOT SETTLED ({', '.join(why)})" if why else ""
        print(f"  {d} {wd.strftime('%a')} {tot[d]:8.2f}  {delta}{flag}")
    print("  same dates line up the monthly free tiers but not the weekdays: allow for the"
          " weekday/weekend shape of your bill and last month's one-offs.")


def check_run_rate(daily):
    phase("3. Run rate and month projection (usage only, settled days, tax added at the end)")
    ok = settled_days(daily)
    month_start = TODAY.replace(day=1).isoformat()
    # Skip days 1-5: monthly free tiers make them cheap.
    basis = [d for d in ok if d >= (TODAY.replace(day=1) + dt.timedelta(days=5)).isoformat()][-10:]
    if len(basis) < 5:
        basis = [d for d in ok if d < month_start][-10:]
        print("  (early month: basis taken from the end of last month)")
    if not basis:
        print("  no settled days"); return
    rate = sum(sum(daily[d].values()) for d in basis) / len(basis)
    mtd = sum(sum(daily[d].values()) for d in daily if d >= month_start)
    dim = ((TODAY.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)).day
    days_done = len([d for d in daily if d >= month_start])
    proj = mtd + rate * (dim - days_done)
    print(f"  basis {basis[0]}..{basis[-1]} ({len(basis)} settled days): ${rate:.2f}/day usage")
    print(f"  month to date usage ${mtd:.2f} over {days_done} days")
    if TAX:
        print(f"  projected month: usage ${proj:,.0f} + tax ${proj * TAX:,.0f} = ${proj * (1 + TAX):,.0f}")
        print(f"  steady month at this rate: ${rate * 30:,.0f} + tax = ${rate * 30 * (1 + TAX):,.0f}")
    else:
        print(f"  projected month: usage ${proj:,.0f}")
        print(f"  steady month at this rate: ${rate * 30:,.0f}")
    print("  caveat: lines still inside a monthly free tier (CloudFront 1 TB, data transfer 100 GB)"
          " read ~$0 in the basis and are under-projected. Add them by hand.")
    return basis


def check_services(daily):
    phase("3b. Per-service: this month's settled days vs the same dates last month")
    month_start = TODAY.replace(day=1).isoformat()
    pairs = [(d, same_date_last_month(dt.date.fromisoformat(d))) for d in settled_days(daily) if d >= month_start]
    pairs = [(d, t.isoformat()) for d, t in pairs if t and t.isoformat() in daily]
    if not pairs:
        print("  no settled days this month yet"); return
    svcs = {s for d, t in pairs for s in (*daily[d], *daily[t])}
    rows = [(s, sum(daily[t].get(s, 0) for _, t in pairs), sum(daily[d].get(s, 0) for d, _ in pairs)) for s in svcs]
    rows.sort(key=lambda r: -abs(r[2] - r[1]))
    print(f"  {pairs[0][0][5:]}..{pairs[-1][0][5:]} vs {pairs[0][1][5:]}..{pairs[-1][1][5:]}"
          f" ({len(pairs)} days each), biggest movers first")
    print(f"  {'service':44} {'last mo':>8} {'this mo':>8} {'diff':>8}")
    for s, a, b in rows[:15]:
        print(f"  {s[:44]:44} {a:8.2f} {b:8.2f} {b - a:+8.2f}")
    a, b = sum(r[1] for r in rows), sum(r[2] for r in rows)
    print(f"  {'TOTAL':44} {a:8.2f} {b:8.2f} {b - a:+8.2f}  ({(b - a) / len(pairs):+.2f}/day)")
    print("  caveat: strip last month's one-offs on these dates (notes.md) before calling a line a trend.")


def check_forecast():
    phase("3c. Console forecast, for comparison only (it excludes most tax)")
    end = ((TODAY.replace(day=28) + dt.timedelta(days=4)).replace(day=1))
    if TODAY >= end:
        return
    d = aws("ce", "get-cost-forecast", "--time-period", f"Start={TODAY},End={end}",
            "--granularity", "MONTHLY", "--metric", "UNBLENDED_COST")
    if "_error" in d:
        return
    print(f"  AWS forecast for {TODAY.strftime('%b')}: ${float(d['Total']['Amount']):,.0f}"
          " (MONTHLY snaps to the whole calendar month, actuals included)")


def check_aws_anomalies():
    phase("4a. AWS Cost Anomaly Detection findings, last 30 days (optional permission)")
    d = aws("ce", "get-anomalies", "--date-interval",
            f"StartDate={TODAY - dt.timedelta(days=30)},EndDate={TODAY}")
    if "_error" in d:
        print("  (needs ce:GetAnomalies + ce:GetAnomalyMonitors; the scan below covers it either way)")
        return
    for a in d.get("Anomalies", []):
        causes = "; ".join(f"{r.get('Service')} {r.get('UsageType', '')}" for r in a.get("RootCauses", [])[:3])
        print(f"  {a['AnomalyStartDate'][:10]}..{a.get('AnomalyEndDate', '')[:10]} "
              f"impact ${a['Impact']['TotalImpact']:.2f}  {causes}")
    if not d.get("Anomalies"):
        print("  none reported")


def check_anomalies(daily, top=6):
    """Per-service scan: each settled day vs the median of up to 7 earlier settled days in the
    same month, never days 1-5 (free tiers make them cheap) and never last month. Days with fewer
    than 3 such days (1-8) are not scanned; section 3b covers them against the same dates last month.
    Flags |delta| >= $3 and >= 30%. Consecutive flags in the same direction become one event:
    a SPIKE if the service returns to baseline, a STEP if it stays at the new level."""
    phase("4b. Cost anomalies by service, last 30 days (spikes and step changes)")
    ok = settled_days(daily)
    window = [d for d in ok if d >= (TODAY - dt.timedelta(days=30)).isoformat()]
    base_days = {d: [p for p in ok if p < d and p[:7] == d[:7] and p[8:] > "05"][-7:] for d in window}
    svcs = {s for d in daily for s in daily[d]}
    events = []
    for s in svcs:
        run = None
        for d in window:
            prior = [daily[p].get(s, 0) for p in base_days[d]]
            if len(prior) < 3:
                continue
            base = sorted(prior)[len(prior) // 2]
            v = daily[d].get(s, 0)
            delta = v - base
            hit = abs(delta) >= 3 and abs(delta) >= 0.3 * max(base, 0.01)
            if hit and run and (delta > 0) == (run["delta"] > 0):
                run["days"].append(d); run["excess"] += delta
            elif hit:
                if run:
                    events.append(run)
                run = {"svc": s, "days": [d], "base": base, "delta": delta, "excess": delta}
            elif run:
                events.append(run); run = None
        if run:
            run["open"] = True
            events.append(run)
    for e in events:
        last = e["days"][-1]
        after = [daily[d].get(e["svc"], 0) for d in ok if d > last][:3]
        back = after and all(abs(a - e["base"]) < max(3, 0.3 * e["base"]) for a in after)
        if back:
            e["kind"] = "SPIKE"
        elif len(e["days"]) >= 3:
            e["kind"] = "STEP"
        elif e.get("open"):
            e["kind"] = "NEW"      # started in the last 1-2 settled days; too early to call
        else:
            e["kind"] = "SPIKE?"
    events.sort(key=lambda e: -abs(e["excess"]))
    print("  days 1-8 of a month are not scanned (no same-month baseline yet); see section 3b for them.")
    if not events:
        print("  none"); return
    print(f"  {'service':36} {'kind':6} {'days':23} {'baseline':>8} {'excess $':>9}")
    for e in events[:15]:
        span = e["days"][0] if len(e["days"]) == 1 else f"{e['days'][0]}..{e['days'][-1][5:]}"
        print(f"  {e['svc'][:36]:36} {e['kind']:6} {span:23} {e['base']:8.2f} {e['excess']:+9.2f}")
    phase("4c. Usage-type breakdown of the biggest anomalies (the WHY)")
    for e in events[:top]:
        d0 = e["days"][0]
        prev = reference_day(dt.date.fromisoformat(d0)).isoformat()
        rows = {}
        for day in (prev, d0):
            r = aws("ce", "get-cost-and-usage", "--time-period",
                    f"Start={day},End={dt.date.fromisoformat(day) + dt.timedelta(days=1)}",
                    "--granularity", "DAILY", "--metrics", "UnblendedCost",
                    "--filter", json.dumps({"And": [
                        {"Dimensions": {"Key": "SERVICE", "Values": [e["svc"]]}},
                        {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}]}),
                    "--group-by", "Type=DIMENSION,Key=USAGE_TYPE")
            for x in r.get("ResultsByTime", [{}])[0].get("Groups", []):
                rows.setdefault(x["Keys"][0], {})[day] = float(x["Metrics"]["UnblendedCost"]["Amount"])
        moved = sorted(rows.items(), key=lambda kv: -abs(kv[1].get(d0, 0) - kv[1].get(prev, 0)))[:3]
        print(f"  {e['svc']} {e['kind']} {d0} (vs {prev}):")
        for ut, v in moved:
            print(f"    {ut:44} {v.get(prev, 0):8.2f} -> {v.get(d0, 0):8.2f}")
    print("  next: for a resource-level answer (which bucket / which load balancer / which instance),"
          " run cur_query.py <month> service <product code>.")


def check_public_ipv4():
    phase("5. Public IPv4 ($0.005/h = ~$3.65/mo each) and idle Elastic IPs")
    enis = aws("ec2", "describe-network-interfaces").get("NetworkInterfaces", [])
    kinds = {}
    for e in enis:
        n = sum(1 for p in e.get("PrivateIpAddresses", []) if p.get("Association"))
        if not n:
            continue
        if e.get("Attachment", {}).get("InstanceId"):
            k = "EC2 instance"
        else:
            k = e.get("InterfaceType", "")
            desc = e.get("Description", "")
            k = "load balancer" if desc.startswith("ELB") else "RDS" if "RDS" in desc else \
                "ECS task" if "ecs" in desc else k or "other"
        kinds[k] = kinds.get(k, 0) + n
    total = sum(kinds.values())
    print(f"  {total} public IPv4 in use = ~${total * 3.65:.0f}/mo: " +
          ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
    eips = aws("ec2", "describe-addresses").get("Addresses", [])
    idle = [a["PublicIp"] for a in eips if "AssociationId" not in a]
    print(f"  Elastic IPs {len(eips)}, unattached {len(idle)} {idle if idle else ''}")


def check_ebs():
    phase("6. Unattached EBS volumes")
    vols = aws("ec2", "describe-volumes", "--filters", "Name=status,Values=available").get("Volumes", [])
    gb = sum(v["Size"] for v in vols)
    p = float(CFG["price_ebs_gp3_gb_month"])
    print(f"  unattached volumes: {len(vols)} ({gb} GB, ~${gb * p:.0f}/mo at ${p}/GB gp3)")
    for v in vols[:10]:
        print(f"    {v['VolumeId']} {v['Size']} GB {v['VolumeType']} created {v['CreateTime'][:10]}")


def check_idle_lbs():
    phase("7. Load balancers: requests over 7 days (idle ALB = ~$16-20/mo + 2 IPs each)")
    lbs = aws("elbv2", "describe-load-balancers").get("LoadBalancers", [])
    end = dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None)
    start = end - dt.timedelta(days=7)
    for lb in lbs:
        if lb.get("Type") != "application":
            print(f"  {lb['LoadBalancerName']:40} type {lb.get('Type')} (not checked)")
            continue
        dim = lb["LoadBalancerArn"].split("loadbalancer/")[1]
        m = aws("cloudwatch", "get-metric-statistics", "--namespace", "AWS/ApplicationELB",
                "--metric-name", "RequestCount", "--dimensions", f"Name=LoadBalancer,Value={dim}",
                "--start-time", start.isoformat(), "--end-time", end.isoformat(),
                "--period", "604800", "--statistics", "Sum")
        n = sum(p["Sum"] for p in m.get("Datapoints", []))
        flag = "  <-- IDLE" if n < 100 else ""
        print(f"  {lb['LoadBalancerName']:40} {n / 7:12,.0f} req/day{flag}")


def check_logs():
    p = float(CFG["price_logs_ingest_gb"])
    phase(f"8. CloudWatch log ingestion by group, 7 days (~${p}/GB ingested)")
    groups, tok = [], None
    while True:
        d = aws("logs", "describe-log-groups", *(["--starting-token", tok] if tok else []))
        groups += d.get("logGroups", [])
        tok = d.get("NextToken")
        if not tok:
            break
    end = dt.datetime.combine(TODAY, dt.time())
    start = end - dt.timedelta(days=7)
    got = {}
    for i in range(0, len(groups), 400):
        chunk = groups[i:i + 400]
        q = [{"Id": f"m{j}", "MetricStat": {"Metric": {"Namespace": "AWS/Logs", "MetricName": "IncomingBytes",
              "Dimensions": [{"Name": "LogGroupName", "Value": g["logGroupName"]}]},
              "Period": 604800, "Stat": "Sum"}} for j, g in enumerate(chunk)]
        d = aws("cloudwatch", "get-metric-data", "--metric-data-queries", json.dumps(q),
                "--start-time", start.isoformat() + "Z", "--end-time", end.isoformat() + "Z")
        for r in d.get("MetricDataResults", []):
            got[chunk[int(r["Id"][1:])]["logGroupName"]] = sum(r["Values"])
        print(f"  ...{min(i + 400, len(groups))}/{len(groups)} groups read", flush=True)
    tot = sum(got.values())
    print(f"  total {tot / 7 / 1e9:.2f} GB/day = ~${tot / 7 / 1e9 * 30 * p:.0f}/mo")
    for k, v in sorted(got.items(), key=lambda x: -x[1])[:8]:
        print(f"    {v / 7 / 1e6:7.0f} MB/day ${v / 7 / 1e9 * 30 * p:6.2f}/mo  {k}")
    no_ret = [g["logGroupName"] for g in groups if not g.get("retentionInDays") and got.get(g["logGroupName"], 0) > 0]
    print(f"  groups still receiving logs with NO retention (grow forever): {len(no_ret)}")
    for g in no_ret[:8]:
        print(f"    {g}")


def check_s3_tiering():
    phase("9. S3 storage class on the watched buckets (Intelligent-Tiering progress)")
    buckets = CFG["s3_buckets_to_watch"]
    if not buckets:
        print("  no buckets configured (config.json: s3_buckets_to_watch)"); return
    end = dt.datetime.combine(TODAY, dt.time())
    start = end - dt.timedelta(days=3)
    for b in buckets:
        parts = []
        for st in ["StandardStorage", "IntelligentTieringFAStorage", "IntelligentTieringIAStorage",
                   "IntelligentTieringAIAStorage"]:
            m = aws("cloudwatch", "get-metric-statistics", "--namespace", "AWS/S3", "--metric-name",
                    "BucketSizeBytes", "--dimensions", f"Name=BucketName,Value={b}",
                    f"Name=StorageType,Value={st}", "--start-time", start.isoformat(),
                    "--end-time", end.isoformat(), "--period", "86400", "--statistics", "Average")
            pts = sorted(m.get("Datapoints", []), key=lambda p: p["Timestamp"])
            if pts:
                parts.append(f"{st.replace('IntelligentTiering', 'IT-').replace('Storage', '')} "
                             f"{pts[-1]['Average'] / 1e12:.2f} TB")
        print(f"  {b}: {', '.join(parts) or 'no data'}")
    print("  IT-FA costs the same as Standard. Saving starts at IT-IA (30 days unread) and IT-AIA (90 days).")


def check_ecr():
    phase("10. ECR repositories without a lifecycle policy")
    from concurrent.futures import ThreadPoolExecutor
    repos = aws("ecr", "describe-repositories").get("repositories", [])
    names = [r["repositoryName"] for r in repos]
    with ThreadPoolExecutor(16) as ex:
        res = list(ex.map(lambda n: aws("ecr", "get-lifecycle-policy", "--repository-name", n), names))
    missing = [n for n, p in zip(names, res) if "_error" in p]
    print(f"  {len(repos)} repos, {len(missing)} without lifecycle policy: {', '.join(missing[:15])}")


def check_container_insights():
    phase("11. ECS Container Insights setting per cluster")
    arns = aws("ecs", "list-clusters").get("clusterArns", [])
    if not arns:
        return
    d = aws("ecs", "describe-clusters", "--clusters", *arns, "--include", "SETTINGS")
    for c in d.get("clusters", []):
        s = next((x["value"] for x in c.get("settings", []) if x["name"] == "containerInsights"), "?")
        print(f"  {c['clusterName']:32} {s:9} running tasks {c.get('runningTasksCount', 0)}")
    print("  'enhanced' on an idle cluster costs ~$0; only running tasks emit.")


def selftest():
    d = dt.date
    assert same_date_last_month(d(2026, 10, 5)) == d(2026, 9, 5)
    assert same_date_last_month(d(2026, 3, 30)) is None
    assert reference_day(d(2026, 10, 20)) == d(2026, 10, 13)      # same month, past day 5
    assert reference_day(d(2026, 10, 10)) == d(2026, 9, 10)       # week earlier would be day 3
    assert reference_day(d(2026, 10, 3)) == d(2026, 9, 3)         # week earlier is last month
    noon = dt.datetime(2026, 10, 20, 12, 0)
    assert settled_by_time(d(2026, 10, 19), noon)
    assert not settled_by_time(d(2026, 10, 19), noon - dt.timedelta(minutes=1))
    assert not settled_by_time(d(2026, 10, 20), noon)
    # read-only guard: every AWS operation this file calls must be a get/describe/list
    ops = set(re.findall(r'aws\("([a-z0-9]+)", "([a-z-]+)"', open(__file__, encoding="utf-8").read()))
    bad = [f"{s} {o}" for s, o in ops if not o.startswith(("get-", "describe-", "list-"))]
    assert not bad, f"non read-only AWS calls: {bad}"
    print(f"selftest ok ({len(ops)} AWS operations, all read-only)")


def main():
    only = set(sys.argv[1:])
    if only == {"selftest"}:
        selftest(); return
    label = CFG.get("account_alias") or (CFG["profile"] or "default profile")
    print(f"# AWS cost point checks, {TODAY}, {label} (read-only)", flush=True)
    # 70 days: the same dates last month for up to 31 days of this month, plus the anomaly window.
    phase("0. Pulling 70 days of daily usage by service from Cost Explorer")
    daily = ce_daily_by_service(TODAY - dt.timedelta(days=70), TODAY)
    if not daily:
        print("  nothing returned. ce:GetCostAndUsage is the one mandatory permission; see iam_policy.json.")
        return
    print(f"  {len(daily)} days")
    checks = [
        ("months", check_months),
        ("daily", lambda: check_daily(daily)),
    ]
    for name, fn in checks:
        if not only or name in only:
            fn()
    if not only or "rate" in only:
        check_run_rate(daily)
        check_services(daily)
        check_forecast()
    if not only or "anomalies" in only:
        check_aws_anomalies()
        check_anomalies(daily)
    for name, fn in [("ipv4", check_public_ipv4), ("ebs", check_ebs), ("lbs", check_idle_lbs),
                     ("logs", check_logs), ("s3", check_s3_tiering), ("ecr", check_ecr),
                     ("ecs", check_container_insights)]:
        if not only or name in only:
            fn()
    print(f"\n# done in {time.time() - T0:.0f}s")


if __name__ == "__main__":
    main()
