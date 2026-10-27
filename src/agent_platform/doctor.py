"""`doctor <env>` (PLAN.md section 12). Each check prints pass, warn, or fail."""

import subprocess
import sys

from . import ssh
from .cli import FAILED, MARKER, console_tail, ssm_online

PASS, WARN, FAIL = "pass", "warn", "fail"

# Remote half: runs as the developer on the box and prints `status|check|detail`.
REMOTE = r"""
say() { printf '%s|%s|%s\n' "$1" "$2" "$3"; }
if [ -f MARKER ]; then say pass "bootstrap marker" "$(head -1 MARKER)"
  grep '^warning:' MARKER | while read -r w; do say warn "bootstrap" "$w"; done
elif [ -f FAILED ]; then say fail "bootstrap marker" "failed: $(cat FAILED)"
else say fail "bootstrap marker" "missing (still running?)"; fi

if systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent.service; then say pass "ssm agent" "running"
else say fail "ssm agent" "snap service not active"; fi
if snap list amazon-ssm-agent 2>/dev/null | tail -1 | grep -q held; then say pass "ssm agent refresh hold" "held"
else say warn "ssm agent refresh hold" "not held; a background refresh will drop sessions"; fi

for url in https://cli.kiro.dev https://claude.ai https://herdr.dev https://astral.sh \
    https://ssm.REGION.amazonaws.com https://ssmmessages.REGION.amazonaws.com https://ec2messages.REGION.amazonaws.com; do
  if curl -sS -o /dev/null --max-time 10 "$url" 2>/dev/null; then say pass "reach $url" ""
  else say fail "reach $url" "no HTTPS response"; fi
done
if curl -sS -o /dev/null --max-time 10 https://gitlab.com 2>/dev/null; then say pass "reach gitlab.com" ""
else say warn "reach gitlab.com" "unreachable (not needed in v1)"; fi

for tool in kiro-cli claude herdr uv git docker; do
  p=$(bash -lc "command -v $tool" 2>/dev/null)
  if [ -n "$p" ]; then say pass "on PATH: $tool" "$p"; else say fail "on PATH: $tool" "not found in a login shell"; fi
done
if id -nG | grep -qw docker; then say pass "docker group" ""; else say warn "docker group" "user not in docker group"; fi
root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)
case "$root" in
  "$HOME"/*) say pass "docker data on home volume" "$root" ;;
  "") say fail "docker data on home volume" "docker info failed" ;;
  *) say warn "docker data on home volume" "$root (lost on destroy)" ;;
esac

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ]; then say pass "linger" "enabled"
else say fail "linger" "disabled; herdr will die on logout"; fi
if mountpoint -q "$HOME" && [ "$(findmnt -n -o SOURCE "$HOME")" != "$(findmnt -n -o SOURCE /)" ]; then
  say pass "home volume" "$(findmnt -n -o SOURCE "$HOME") at $HOME"
else say fail "home volume" "$HOME is not a separate mount"; fi
if systemctl is-active --quiet idle-check.timer; then say pass "idle-check timer" "active"
else say fail "idle-check timer" "not active"; fi

[ -f ~/.agent-platform/env ] && . ~/.agent-platform/env
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then say pass "claude token" "CLAUDE_CODE_OAUTH_TOKEN set"
elif [ -s ~/.claude/.credentials.json ]; then say pass "claude token" "~/.claude/.credentials.json"
else say warn "claude token" "none; set CLAUDE_CODE_OAUTH_TOKEN in home.local/.agent-platform/env and sync"; fi
if [ -n "${KIRO_API_KEY:-}" ]; then say pass "kiro token" "KIRO_API_KEY set"
elif [ -s ~/.local/share/kiro-cli/data.sqlite3 ]; then say pass "kiro token" "data.sqlite3"
else say warn "kiro token" "none; set KIRO_API_KEY in home.local/.agent-platform/env and sync"; fi
if out=$(timeout 30 bash -lc 'claude auth status' 2>&1); then say pass "claude signed in" "$(echo "$out" | jq -r .authMethod 2>/dev/null)"
else say fail "claude signed in" "claude auth status: not logged in"; fi
if out=$(timeout 30 bash -lc 'kiro-cli whoami' 2>&1); then say pass "kiro signed in" "$(echo "$out" | head -1 | head -c 80)"
else say fail "kiro signed in" "$(echo "$out" | tail -1 | head -c 120)"; fi
"""

# Run with a tty: idle signal 3 needs SSH-over-SSM logins in logind with a tty.
TTY_CHECK = 'loginctl show-session "$XDG_SESSION_ID" -p Class -p TTY 2>&1 | tr "\\n" " "'


def simulate_checks(ctx):
    """Simulate the developer policy (catches a silently removed policy)."""
    a = ctx.account
    role = a.developer_policy_role_arn or a.developer_role_arn
    if not role:
        return [(WARN, "policy simulation", "no developer_policy_role_arn in the account file")]
    acct = role.split(":")[4]
    ec2 = f"arn:aws:ec2:{a.region}:{acct}"
    tagged = {
        "aws:ResourceTag/agent-platform": "true",
        "aws:RequestTag/agent-platform": "true",
        "ssm:resourceTag/agent-platform": "true",
    }
    cases = [
        ("ec2:RunInstances", f"{ec2}:instance/*"),
        ("ec2:CreateVolume", f"{ec2}:volume/*"),
        ("ec2:StartInstances", f"{ec2}:instance/i-0"),
        ("ec2:StopInstances", f"{ec2}:instance/i-0"),
        ("ec2:TerminateInstances", f"{ec2}:instance/i-0"),
        ("ec2:AttachVolume", f"{ec2}:volume/vol-0"),
        ("ec2:GetConsoleOutput", f"{ec2}:instance/i-0"),
        ("ssm:StartSession", f"{ec2}:instance/i-0"),
        ("ssm:StartSession", f"arn:aws:ssm:{a.region}::document/AWS-StartSSHSession"),
        ("s3:PutObject", f"arn:aws:s3:::{a.state_bucket}/env/x"),
        ("s3:ListBucket", f"arn:aws:s3:::{a.state_bucket}"),
    ]
    context = [
        {"ContextKeyName": k, "ContextKeyValues": [v], "ContextKeyType": "string"} for k, v in tagged.items()
    ] + [
        {"ContextKeyName": "ssm:SessionDocumentAccessCheck", "ContextKeyValues": ["true"], "ContextKeyType": "boolean"},
        {"ContextKeyName": "s3:prefix", "ContextKeyValues": ["env/x"], "ContextKeyType": "string"},
        # Without it, region-restricting SCPs make the simulator report explicitDeny.
        {"ContextKeyName": "aws:RequestedRegion", "ContextKeyValues": [a.region], "ContextKeyType": "string"},
    ]
    iam = ctx.aws.client("iam")
    results = []
    for action, resource in cases:
        r = iam.simulate_principal_policy(
            PolicySourceArn=role, ActionNames=[action], ResourceArns=[resource], ContextEntries=context
        )["EvaluationResults"][0]
        ok = r["EvalDecision"] == "allowed"
        if not ok and r.get("OrganizationsDecisionDetail", {}).get("AllowedByOrganizations") is False:
            # The simulator applies SCPs even where they do not bind (a management account).
            results.append((WARN, f"policy allows {action}", "denied by an Organizations SCP in the simulator"))
            continue
        results.append((PASS if ok else FAIL, f"policy allows {action}", "" if ok else r["EvalDecision"]))
    # The default shell document must stay denied: SSH is forced.
    r = iam.simulate_principal_policy(
        PolicySourceArn=role,
        ActionNames=["ssm:StartSession"],
        ResourceArns=[f"arn:aws:ssm:{a.region}:{acct}:document/SSM-SessionManagerRunShell"],
        ContextEntries=context,
    )["EvaluationResults"][0]
    denied = r["EvalDecision"] != "allowed"
    results.append((PASS if denied else FAIL, "policy denies shell sessions", "" if denied else "allowed"))
    return results


def mnt_refs(ctx):
    hits = []
    for d in (ctx.repo / "home", ctx.repo / "home.local"):
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p == ctx.repo / "home" / "README.md":
                continue  # not synced
            if p.is_file() and p.stat().st_size < 1_000_000:
                try:
                    if "/mnt/" in p.read_text(errors="strict"):
                        hits.append(str(p.relative_to(ctx.repo)))
                except (UnicodeDecodeError, OSError):
                    pass
    return hits


def doctor(ctx, env):
    results = []

    def add(status, check, detail=""):
        results.append((status, check, detail))
        print(f"[{status}] {check}" + (f": {detail}" if detail else ""), flush=True)

    try:
        ident = ctx.aws.base.client("sts").get_caller_identity()
        add(PASS, "aws credentials", ident["Arn"])
    except Exception as e:  # noqa: BLE001 - report any credential problem as a failure
        add(FAIL, "aws credentials", str(e))
        return 1
    if ctx.account.role_mode == "create":
        try:
            ctx.aws.session.client("sts").get_caller_identity()
            add(PASS, "developer role assumable", ctx.account.developer_role_arn)
        except Exception as e:  # noqa: BLE001
            add(FAIL, "developer role assumable", str(e))
            return 1
    try:
        for r in simulate_checks(ctx):
            add(*r)
    except Exception as e:  # noqa: BLE001
        add(FAIL, "policy simulation", str(e))

    inst = ctx.find_instance(env)
    if not inst or inst["State"]["Name"] != "running":
        add(FAIL, "instance running", inst["State"]["Name"] if inst else "no instance")
        return 1
    add(PASS, "instance running", f"{inst['InstanceId']} {inst['InstanceType']}")
    iid = inst["InstanceId"]
    if ssm_online(ctx, iid):
        add(PASS, "ssm online")
    else:
        add(FAIL, "ssm online", "SSM does not report the instance online")
        return 1

    alias = ctx.alias(env)
    script = REMOTE.replace("FAILED", FAILED).replace("MARKER", MARKER).replace("REGION", ctx.account.region)
    try:
        r = ssh.run(alias, script, timeout=240)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode not in (0, 1) or not r.stdout:
        add(FAIL, "ssh over ssm", (r.stderr.strip() if r else "timed out") or "no output")
        print(console_tail(ctx, iid), file=sys.stderr)
        return 1
    add(PASS, "ssh over ssm", alias)
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0] in (PASS, WARN, FAIL):
            add(*parts)
            if parts[1] == "bootstrap marker" and parts[0] == FAIL:
                print(console_tail(ctx, iid), file=sys.stderr)

    t = ssh.run(alias, TTY_CHECK, timeout=60, tty=True)
    out = t.stdout.replace("\r", "").strip()
    has_tty = "TTY=pts/" in out and "Class=user" in out
    add(PASS if has_tty else FAIL, "ssh session in logind with a tty", out or t.stderr.strip())

    hits = mnt_refs(ctx)
    add(WARN if hits else PASS, "no /mnt/ paths in synced files", ", ".join(hits))

    return 1 if any(s == FAIL for s, _, _ in results) else 0
