---
name: aws-cost-audit
description: Read-only AWS cost audit for the user's AWS account. Use when the user asks about the AWS bill, "check costs", "why did cost go up", "what's the forecast", "is X costing money", "any anomalies", "which bucket / load balancer / DB costs most", "is Intelligent-Tiering working", or mentions the CUR / Cost and Usage Report. Runs point checks across the whole account (monthly bill with tax, settled daily trend vs the same dates last month, run rate and projection, 30-day anomaly scan with usage-type breakdown, public IPv4, idle load balancers, unattached EBS, log ingestion, S3 tiering, ECR lifecycle, Container Insights) and answers "which resource" questions from the CUR parquet. Never writes to AWS.
---

# AWS cost audit

Read `README.md` in this folder before interpreting any number. Section 3 (reading a daily number)
and section 4 (surprises) are the traps that make most cost readings wrong.

## Hard rules

- **Read-only.** Only Describe/Get/List calls. Never change an AWS resource. If a check suggests a
  saving, report it with the number and the risk; the user makes the change.
- Never read `~/.aws`. Use the profile name from `config.json` and let the AWS CLI resolve it.
- Anything that may run >30 s goes to a log file (`cmd > file.log 2>&1`), then read the file.
- CloudWatch Logs reads cost money: state the log group's daily volume and the window first, and
  ask before scanning more than a day of any access log.
- Quote numbers as measured vs derived, and say which days are the basis.
- If a shell hook rewrites command output (for example rtk), wrap raw `aws … --output json` calls
  that you pipe into a parser so the hook passes them through, or call them from Python as the
  scripts do.

## 0. First run: ask, then set up (before any AWS call)

If `config.json` does not exist next to this file, do this first. Ask all of the questions in one
message, with the permissions table in the same message, so the user answers once.

**Questions**

1. Which AWS CLI profile and region? (empty profile = default credentials)
2. Does the invoice carry tax on top of usage, and at what rate? Accounts billed by AWS India
   pay 18% GST; most AWS Inc. accounts pay 0% or a local VAT. Enter the rate as a decimal.
3. Is there a CUR 2.0 data export (parquet, hourly, with resource IDs)? If yes, its
   `s3://bucket/prefix/data` path. If no, the "which resource" questions fall back to usage types
   only (README section 2 says how to create one).
4. Which S3 buckets, if any, hold bulk data worth watching for Intelligent-Tiering progress?
5. Optional: is there a service that bills exactly the same amount every day (a fixed-size
   ElastiCache node, for example)? Its Cost Explorer service name and daily amount make a second
   "is this day settled" tell.

**Permissions** (the full policy is `iam_policy.json`; each statement's `Sid` says which group)

| Group | Actions | Unlocks | Without it |
|---|---|---|---|
| **Mandatory** | `ce:GetCostAndUsage`, `ce:GetCostForecast` | Sections 0 to 4c: bill, daily trend, run rate, projection, anomaly scan | Nothing works. The script stops after section 0. |
| Optional: anomaly detection | `ce:GetAnomalies`, `ce:GetAnomalyMonitors` | Section 4a, AWS's own anomaly findings | 4b covers the same ground with our scan. |
| Optional: inventory | EC2, ELB, CloudWatch, Logs, ECR, ECS describe/get/list (see policy) | Sections 5 to 11: public IPv4, EBS, idle ALBs, log ingestion, S3 tiering, ECR, Container Insights | Those sections print the AccessDenied line and move on. |
| Optional: CUR | `s3:ListBucket` + `s3:GetObject` on the export prefix | `cur_query.py`: which bucket, which load balancer, which instance | Resource-level questions stay unanswered. |

Tell the user that Cost Explorer calls cost $0.01 each and a full run makes 10 to 20 of them.
Everything else is free. The user attaches the policy; never create or change IAM yourself.

**Then** write `config.json` from the answers (`config.example.json` is the template), create an
empty `notes.md` for this account's one-offs and reference numbers, and go on to the procedure.
Both files are gitignored and stay on the user's machine.

## Procedure

1. **Run the point checks** (about 1 min):
   ```bash
   python3 -u <this folder>/scripts/point_checks.py > <scratchpad>/audit.log 2>&1
   ```
   Run it in the foreground with a 5 minute timeout, then read the log once. Don't poll.
   For a narrow question run only the section: `months daily rate anomalies ipv4 ebs lbs logs s3 ecr ecs`.
   Any `aws <service> <op>: AccessDenied` line in the log names the missing action. Quote it and
   point at the matching statement in `iam_policy.json`.

2. **Read it with the traps applied:**
   - Any day flagged `NOT SETTLED` is partial. Don't use it. A Cost Explorer day is final after
     D+1 12:00 UTC.
   - **Across months, compare the same dates** (the 3rd to 6th of this month vs the 3rd to 6th of
     last month). Never compare a month's first days with the previous month's last days: free
     tiers reset on the 1st, so early days read cheap for reasons that have nothing to do with usage.
   - The script follows this: section 2 compares a day with a week earlier inside the month, and
     with the same date last month across the boundary; 3b compares this month's days with the
     same dates last month; 4b never compares across months.
   - Same dates land on different weekdays, so allow for the bill's weekly shape, and strip last
     month's one-offs on those dates (`notes.md`) before calling a line a trend.
   - Days 1 to 5 of a month are cheap on free-tier lines (CloudFront, Lambda, EC2, data transfer
     out). They are never a baseline for later days.
   - Projections are **usage-only × (1 + tax rate)**. Never quote the console forecast or its
     "vs last month %" (README section 4.1).
   - A stable total is not evidence the lines under it are right. Read per-service lines.

3. **Anomalies (last 30 days)**, section 4:
   - 4a tries AWS Cost Anomaly Detection (optional permission).
   - 4b is our own scan: each settled day per service vs the median of up to 7 earlier days in the
     same month (days 1 to 5 never count). Days 1 to 8 aren't scanned; read 3b for them.
     Flags |Δ| ≥ $3 and ≥ 30%. `SPIKE` = came back, `STEP` = new level held ≥ 3 days,
     `NEW` = started in the last 1 to 2 settled days (too early to call), `SPIKE?` = short, unclear.
   - 4c shows which usage types moved for the biggest ones.
   - For each anomaly, give a cause or say it's unexplained. Known causes for this account live in
     `notes.md`. Go to the CUR for "which resource" (step 4) before guessing.

4. **"Which bucket / LB / DB / instance?"**, use the CUR (README section 2):
   ```bash
   pip install -r <this folder>/scripts/requirements.txt
   python3 -u <this folder>/scripts/cur_query.py <YYYY-MM> service <ProductCode> > cur.log 2>&1
   ```
   Modes: `top`, `service <code>`, `daily <code> <resource_id>`, `sql "<query on view cur>"`.
   A CUR export only has data from the month it was created. Earlier months: Cost Explorer only.

5. **Report** in this shape:
   - The answer first (is it high / normal / what changed), with the number and its basis days.
   - A small table of the lines that matter.
   - Anomalies: cause per anomaly, or "unexplained" plus what would confirm it.
   - Savings found: $/mo, what it takes, the risk. Nothing applied.
   - Anything that needs a later re-check, with a date.

## Keep the notes current

When an audit finds a new one-off, a cause for a recurring spike, or a reference number for this
account (normal day, normal month, top lines), write it to `notes.md` in the same session and tell
the user. Traps that apply to every AWS account go in `README.md` instead.
