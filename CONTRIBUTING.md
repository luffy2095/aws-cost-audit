# Contributing

Thank you for helping. This guide says what helps most, the rules every change must follow, and how to test.

## What helps most

1. **A new trap.** A way AWS cost readings mislead people that sections 3 and 4 of the README do not cover yet. Explain it with an example that uses invented numbers.
2. **A new check.** Something worth looking at on every account, for example idle NAT gateways, old EBS snapshots or unused reserved capacity.
3. **Region and tax coverage.** Prices for EBS and log ingestion by region, and how tax appears for sellers other than AWS India.
4. **Cost and Usage Report queries** that answer a common "which resource?" question.
5. **Fixes to the docs.**

For anything bigger than a typo, open an issue first so we agree on the approach before you write it.

## Set up

```bash
git clone https://github.com/<you>/aws-cost-audit
cd aws-cost-audit
python3 scripts/point_checks.py selftest     # date logic and the read-only guard, no AWS calls
```

To try your branch inside Claude Code, link it into your skills folder:

```bash
ln -s "$PWD" ~/.claude/skills/aws-cost-audit
```

## Rules every change must follow

- **Read-only, always.** Every AWS call must be a get, describe or list. The self-test fails on any other operation. Never add a call that creates, changes or deletes anything.
- **Permissions in the same change.** When you add an AWS call, add its IAM action to `iam_policy.json` in the right statement. Only Cost Explorer reads are mandatory. Everything else goes in an `Optional...` statement, and the check must print the AccessDenied line and move on when it is missing.
- **State the cost.** Cost Explorer calls cost $0.01 each. Say in the pull request how many calls your change adds per run. CloudWatch Logs queries are billed per GB scanned, so size them first.
- **No account data.** Never commit account IDs, ARNs, bucket names, resource names or real bill amounts. Use invented examples. Account-specific knowledge belongs in each user's own `notes.md`, which is gitignored.
- **Every number is computed.** An example number in the README must come from real arithmetic, not a guess.
- **Print before slow work.** Each check starts with a `phase()` line, so a slow call is never silent.

## Add a check

1. Write a `check_<name>()` function in `scripts/point_checks.py`. Start it with `phase("<n>. <what it checks>")`.
2. Use the `aws()` helper for every call. It adds the profile and region and turns errors into one printed line.
3. Add the check to the list in `main()` with a short section name, so `point_checks.py <name>` runs it alone.
4. Add its IAM actions to `iam_policy.json`.
5. Add a row to the table in README section 1, and the section name to the procedure in `SKILL.md`.
6. Run `python3 scripts/point_checks.py selftest`, then run your section against an account you own.

## Pull requests

Keep each pull request to one change. The template lists the checks.
