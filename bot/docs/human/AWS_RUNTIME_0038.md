> **SUPERSEDED by 0042 (`FLY_RUNTIME_0042.md`).** The stack below is 27
> CloudFormation resources for a process that makes three decisions a day and
> needs one outbound TLS connection. Fly does the five things this book
> actually needs from a host in forty lines. Worse, everything here pointed at
> an image that **had never built** (INVENTORY D21). Kept for the VPC/egress
> reasoning and because its checker and tests still pass; not the deployment
> path.

# AWS RUNTIME — the host that can reach the venue

Written 2026-09-13 by 0038. `tools/aws_stack.py` generates the whole stack and
checks it; `bot/tests/test_aws_stack.py` checks the checker.

Nothing here has been deployed. The template is code, not evidence.

## What it builds

One VPC. One Fargate task. Nothing that can accept a connection.

```
public subnet   NAT gateway + elastic IP        <- pin the Bybit key to this
private A/B     the task, no public IP          <- egress 443 and NFS only
EFS             /app/state, encrypted, Retain   <- the 0037 ledger survives
Secrets Manager BYBIT_API_KEY / BYBIT_API_SECRET by ARN, never in the template
CloudWatch      90-day retention
```

Invariants the tests enforce, and the cost of each if it slipped:

| Invariant | If it slips |
|---|---|
| `DesiredCount: 1`, `MaximumPercent: 100` | two engines on one account and one liquidation price — AWS's default 200 puts them side by side on every deploy |
| `AssignPublicIp: DISABLED`, no ingress | the health endpoint is unauthenticated and reports positions and kill-switch state |
| egress 443 + NFS only | a compromised container can reach anything |
| EFS at `/app/state` | Fargate's disk dies with the task and the carry ledger with it |
| secrets by ARN | this repository exists downstream of an image that shipped live keys |
| no wildcard IAM | the task role may mount its own filesystem and nothing else |
| `USE_TESTNET` is a **literal** | mainnet becomes a code change and a review, not a deploy-time argument |

## Egress, honestly

"NAT egress allowlist to the venue only" means AWS Network Firewall with an
FQDN rule group: **about $300/month** plus data processing, against a book
capped at $100. `--network-firewall` generates the rule group and it is OFF by
default. At this size the controls that protect the money are on the key:
withdrawals disabled and IP-pinned to the NAT's elastic IP, which this stack
gives you as a stable address (`NatIpToPinTheKeyTo`).

Running cost of the stack as generated, us-east-1, order of magnitude: NAT
gateway ~$33/month plus data, Fargate 0.5 vCPU/1 GB ~$18/month, EFS a few
cents, CloudWatch a dollar or two. Call it **$55/month to run a $100 book** —
which is the honest arithmetic of a drill, not of a business. The stack is
sized so the same template carries a book a thousand times larger.

## Deploy

```bash
cd bot
python3 tools/aws_stack.py --out /tmp/carry-stack.json      # PASS on every invariant

# 1. the image
aws ecr create-repository --repository-name carry-book
docker build -t carry-book .
docker tag carry-book:latest "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/carry-book:0038"
aws ecr get-login-password | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/carry-book:0038"

# 2. the keys — TESTNET keys, trade permission only, withdrawals OFF
aws secretsmanager create-secret --name bybit/testnet/key    --secret-string "..."
aws secretsmanager create-secret --name bybit/testnet/secret --secret-string "..."

# 3. the stack
aws cloudformation deploy \
  --template-file /tmp/carry-stack.json \
  --stack-name carry-book \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      ImageUri="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/carry-book:0038" \
      ApiKeySecretArn="$(aws secretsmanager describe-secret --secret-id bybit/testnet/key    --query ARN --output text)" \
      ApiSecretArn="$(aws secretsmanager describe-secret --secret-id bybit/testnet/secret --query ARN --output text)" \
      CarryExecutionMode=overlay CarryBorrowApr=0.0

# 4. pin the key to the NAT address, in the Bybit UI
aws cloudformation describe-stacks --stack-name carry-book \
  --query "Stacks[0].Outputs[?OutputKey=='NatIpToPinTheKeyTo'].OutputValue" --output text
```

## The question this stack exists to answer

```bash
python3 tools/aws_stack.py --run-task-command      # prints it with the right shape
```

One task, read-only, no keys used: `connector_check.py --require-bybit-testnet`
from **inside** the VPC. Exit 0 means every public read the book makes answered
200 and the drill can start. Exit 2 means this VPC cannot run it, and nothing
else in Phase D is worth doing until that changes.

`connector_check` has never returned OK from any host in this project's
history. That is the single fact this deployment is for.

## Then, and only then: the drill

The task will hold a real (testnet) position, so before it starts:

1. fund the testnet subaccount with test BTC — the overlay stands aside with
   `NO_SPOT_INVENTORY` if there is none to hedge;
2. confirm `PAPER_TRADING=0`, `USE_TESTNET=1`, cap still $100;
3. watch the first cold start in the log: `carry cold start: CLEAN` on an
   empty ledger and a flat venue.

Then the six recorded steps: open a pair at $100 · observe a funding print
booked · force drift past the band and watch the PERP rebalance · three
negative prints and watch it unwind perp-first · restart the task while
HEDGED and watch it RESUME rather than open a second hedge · the kill-switch
drill, signed.

Every one of those is a log line in the group this stack creates. 0039 turns
them into a ledger and an artefact; until then they are evidence a human
reads.
