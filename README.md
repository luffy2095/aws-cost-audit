# AWS cost audit

A Claude Code skill that reads an AWS bill the way a careful person would: settled days only,
same dates across months, tax applied last, per-service lines instead of totals, and the raw
Cost and Usage Report when the question is "which resource". Read-only. Nothing in here changes
an AWS resource.

## Install

```bash
git clone https://github.com/luffy2095/aws-cost-audit ~/.claude/skills/aws-cost-audit
```

Then ask Claude Code anything about your AWS bill. On the first run the skill asks five setup
questions (profile, region, tax rate, CUR export, buckets to watch) and shows which IAM
permissions are mandatory and which are optional. Answers go to `config.json`, which stays on
your machine.

Requirements: AWS CLI v2 with a configured profile, Python 3.9+. The CUR query needs
`pip install -r scripts/requirements.txt` (DuckDB).

## Permissions

`iam_policy.json` is the complete read-only policy. Attach only the statements you want:

| Statement `Sid` | What it unlocks |
|---|---|
| `MandatoryCostExplorerRead` | Everything in sections 0 to 4c. Without it nothing runs. |
| `OptionalAnomalyDetectionRead` | Section 4a, AWS's own anomaly findings. The script's own scan (4b) works without it. |
| `OptionalInventoryRead` | Sections 5 to 11: public IPv4, unattached EBS, idle load balancers, log ingestion, S3 tiering, ECR lifecycle, Container Insights. |
| `OptionalCurParquetList` + `OptionalCurParquetRead` | `cur_query.py`. Replace `YOUR-CUR-BUCKET` and `YOUR/CUR/PREFIX` with your export's location. |

Cost Explorer API calls are $0.01 each. A full run makes 10 to 20. Every other call is free.

---

## 1. The 60-second check

```bash
python3 -u scripts/point_checks.py > audit.log 2>&1   # about 1 min, read-only
```

It prints, in order:

| # | Check | What it answers |
|---|---|---|
| 1 | Monthly bill, Usage vs Tax | Is the month trending right? Is the tax rate what you expect? |
| 2 | Daily usage, 15 days, vs a week earlier (same date last month across a month boundary) | Did anything jump? Is yesterday settled yet? |
| 3 | Run rate, projection, per-service vs the same dates last month | What will this month and a normal month cost, with tax? Which lines moved? |
| 3c | AWS's own forecast | Shown only to compare. Don't quote it (section 4.1). |
| 4a-c | **Anomalies, last 30 days** | Spikes and step changes per service, and which usage type moved |
| 5 | Public IPv4 count | The whole VPC line is $3.65/mo per public address |
| 6 | Unattached EBS volumes | Paying for disks nobody mounts |
| 7 | Load balancer requests | Idle ALBs cost a fixed hourly fee plus 2 IPs each |
| 8 | Log ingestion by group | Where the CloudWatch bill actually comes from |
| 9 | S3 storage classes on the watched buckets | Is Intelligent-Tiering moving data down? |
| 10 | ECR repos without lifecycle policy | Old images pile up forever |
| 11 | Container Insights per ECS cluster | Enhanced is only billed while tasks run |

Run one section only: `point_checks.py anomalies`, `rate`, `logs`, `s3`, `ipv4`, `lbs`, `ebs`,
`ecr`, `ecs`, `months`, `daily`. `point_checks.py selftest` checks the date logic without AWS calls.

Prices for EBS and log ingestion vary by region. Set them in `config.json`.

---

## 2. What "CUR" is and when to use it

**CUR = Cost and Usage Report** (AWS now calls it a "Data Export", CUR 2.0). It is the raw bill:
one row per resource, per usage type, per hour. Cost Explorer is a summary of it.

**Use the CUR when the question is "which one?"** Cost Explorer can tell you S3 went up. It
cannot tell you *which bucket*, *which load balancer*, *which DB* or *which instance*. The CUR can,
because it carries `line_item_resource_id`.

**Create one** (console: Billing and Cost Management, Data Exports, Create, "Standard data export",
CUR 2.0): tick *Include resource IDs*, choose parquet and hourly granularity, pick a bucket and
prefix. Delivery starts the next day and has no data for earlier months. The path to put in
`config.json` ends in `/data`:
```
s3://<bucket>/<prefix>/data/BILLING_PERIOD=YYYY-MM/<export-name>-0000N.snappy.parquet
```
- Delivered about once a day. A month is split into several parts. Always read every part, and
  re-sync before each query, because AWS rewrites the month's files on each delivery.

**How to query it:**
```bash
pip install -r scripts/requirements.txt
python3 scripts/cur_query.py 2026-09 top                            # top 25 resources by cost
python3 scripts/cur_query.py 2026-09 service AmazonS3               # every bucket x usage type
python3 scripts/cur_query.py 2026-09 daily AmazonS3 my-bucket       # one resource, day by day
python3 scripts/cur_query.py 2026-09 sql "select ... from cur where ..."
```

**Columns that matter:**

| column | meaning |
|---|---|
| `line_item_product_code` | service: `AmazonS3`, `AWSELB`, `AmazonRDS`, `AmazonEC2`, `AmazonAthena`… |
| `line_item_resource_id` | S3 = bucket name, ELB/RDS = ARN, EC2 = instance id |
| `line_item_usage_type` | what was billed: `TimedStorage-ByteHrs`, `DataTransfer-Out-Bytes`… (region-prefixed) |
| `line_item_usage_start_date` | the hour |
| `line_item_usage_amount` / `line_item_unblended_cost` | quantity and dollars |
| `line_item_line_item_type` | `Usage`, `Tax`, `Credit`… filter to `Usage` |

**Traps in the CUR:**
- **Filter on product code, not usage type alone.** `DataTransfer-Out-Bytes` exists on S3, ELB
  *and* EC2 lines.
- **In `sql` mode, don't name columns `old`, `new` or `day`.** DuckDB fails with a parser error
  on those aliases; use `d`, `before`, `after`.
- **Lambda data-out has no function name.** Its `line_item_resource_id` is empty, so the CUR can't
  say which function sent the bytes. Match it per hour against each function's invocation count.
- **`bill_billing_entity = "AWS"` does NOT mean AWS Inc.** That column only separates AWS from
  Marketplace. The legal seller of record is on the Bills page in the console.

---

## 3. How to read a daily number without fooling yourself

1. **Is the day settled?** A Cost Explorer day is final only after **D+1 12:00 UTC**. Before that
   it is partial and looks cheap. The script applies this rule. A second tell: a service that bills
   a fixed amount every day (`settle_tells` in `config.json`) reading below that amount. Don't use
   a monthly fee spread over the month as a tell (WAF, for example): its daily share changes with
   the month's length and flags every settled day in a 31-day month.
2. **Across months, compare the same dates, never a month's start with the previous month's end.**
   Free tiers reset on the 1st (section 4.2), so early-month days read cheap for reasons that have
   nothing to do with usage. Within one month, compare with the same weekday last week (never with
   yesterday) once both days are past day 5. Most bills have a weekly shape (weekday peak, weekend
   trough), and same dates across months land on different weekdays, so allow for it or compare
   whole weeks.
3. **Strip one-offs first.** A lifecycle transition fee, a backfill, a migration day. Keep a list
   in `notes.md`.
4. **Days 1 to 5 of a month read cheap** because monthly free tiers reset. Never build a rate from them.
5. **Check lines, not totals.** Seven service lines can each be wrong by $10 while the total moves
   $3, because the errors cancel.

---

## 4. Surprises that apply to every account

### 4.1 Tax and the console forecast
- When the seller of record charges tax (AWS India adds 18% GST, AWS EMEA adds local VAT), the tax
  is a flat percentage of usage, exact to 4 decimals every month.
- Cost Explorer stamps **the whole month's tax on day 1** and revises that one day all month.
- So the console's **"forecasted cost" leaves out most of the tax**, and its **"vs last month"
  % compares a full month of tax with a partial one**. A month can show +1% while services rose +15%.
- **Right way:** usage-only (`RECORD_TYPE = Usage`), identical day counts, then × (1 + tax rate).
- `get-cost-forecast --granularity MONTHLY` snaps to the whole calendar month *including actuals*;
  use `DAILY` if you want only the remaining days.
- Who the seller of record is decides the tax. `bill_billing_entity` in the CUR does not tell you;
  the Bills page does.

### 4.2 Monthly free tiers make early-month days look cheap
| Line | Allowance | Effect |
|---|---|---|
| CloudFront | 1 TB egress + 10M requests / month | ~$0 until the TB runs out, then real money every day |
| EC2 `t4g.small` (trial) | 750 h / month | several boxes use it up in the first days |
| Lambda | 1M requests + 400k GB-s | first days read about half the later daily rate |
| Data transfer out | 100 GB / month | gone on day 1 for most accounts |

**Section 3's projection leaves out late-month CloudFront.** Its basis days often fall before the
1 TB runs out, so CloudFront reads ~$0 and the rest of the month is projected at ~$0 too. Add it
by hand: projected GB − 1,024 × last month's realised $/billable GB.

### 4.3 Where the money usually is
- **Two pipelines side by side.** An old and a new consumer reading the same Kinesis stream, an
  old search cluster kept "just in case". Pure duplicate spend, often the biggest single saving.
- **Firehose dynamic partitioning charges per object**, not per byte. Tiny parquet files × many
  partitions = the fee.
- **The whole VPC line is public IPv4** at $0.005/h ($3.65/mo) per address. Servers behind an ALB
  still need one for *outbound* calls; removing them needs a NAT, which costs about as much as it saves.
- **CloudWatch is ~all log ingestion.** Storage is ~$0, so retention changes save almost nothing.
  Access logs are usually most of it; drop fields and warning spam at the source.
- **Container Insights "enhanced" costs ~nothing** on clusters whose tasks are mostly stopped.
- **Athena is billed per TB scanned**, and a scheduled `OPTIMIZE` or compaction that rescans the
  hot partitions several times a day climbs through the month and resets on the 1st. Not an anomaly.
  Each Iceberg rewrite also leaves a full copy on S3 until the snapshot expires.
- **ALB data-out follows new connections, not response bytes.** The TLS handshake alone is a few KB
  per connection. Short-lived connections with tiny replies bill like a much bigger service.

### 4.4 S3 Intelligent-Tiering
- Enable it with a lifecycle *rule* (day ≥ 1; the console rejects day 0). Objects move into
  IT-Frequent Access within a day or two.
- **Frequent Access costs the same as Standard.** The saving starts only when objects go 30 days
  unread (IT-Infrequent, about −45%) and 90 days (Archive Instant, about −80%). Section 9 of the
  script shows the TB in each tier so you can watch it happen.
- The transition itself is a one-off per-object fee that shows up as an S3 spike on the day.
- A lifecycle *rule* and an Intelligent-Tiering *configuration* are different things. Removing the
  rule later does not move objects back to Standard.

### 4.5 Things that look dead but aren't
- An old instance plus an old load balancer with a legacy hostname often still serves real traffic.
  Check request counts (section 7) before calling it dead.
- Bucket names say nothing about access. Check `get-bucket-policy-status` before saying "private".

---

## 5. Rules for whoever runs this (people or Claude)

- **Read-only.** Nothing in this skill writes to AWS. Any change (delete, resize, lifecycle,
  log class, SG, IAM) is the account owner's to make, after it's been proposed.
- **Size CloudWatch reads first.** Logs Insights and `filter-log-events` are billed per GB scanned.
  Use one hour, not seven days, on access logs.
- **Anything slower than ~30 s writes to a log file**, then read the file.
- Account-specific knowledge (one-offs, known causes, normal day and month, top lines) goes in
  `notes.md`, which is gitignored. This README stays generic.
