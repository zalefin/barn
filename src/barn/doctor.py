"""`doctor <env>` (PLAN.md section 12). Each check prints pass, warn, or fail."""

import re
import subprocess

from botocore.config import Config

from . import ssh, ui
from .cli import FAILED, MARKER, show_console, ssm_online
from .sync import sources

PASS, WARN, FAIL = "pass", "warn", "fail"

# Remote half: runs as the developer on the box and prints `status|check|detail`.
REMOTE = r"""
say() { printf '%s|%s|%s\n' "$1" "$2" "$3"; }
if [ -f MARKER ]; then say pass "bootstrap marker" "$(head -1 MARKER)"
  grep '^warning:' MARKER | while read -r w; do say warn "bootstrap" "$w"; done
elif [ -f FAILED ]; then say fail "bootstrap marker" "failed: $(cat FAILED)"
else say fail "bootstrap marker" "missing (still running?)"; fi

if [ "$(hostname)" = BOXNAME ]; then say pass "hostname" "$(hostname)"
else say warn "hostname" "$(hostname), expected BOXNAME; \`barn create\` sets it"; fi
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

[ -f ~/.barn/env ] && . ~/.barn/env
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then say pass "claude token" "CLAUDE_CODE_OAUTH_TOKEN set"
elif [ -s ~/.claude/.credentials.json ]; then say pass "claude token" "~/.claude/.credentials.json"
else say warn "claude token" "none; set CLAUDE_CODE_OAUTH_TOKEN in SYNCDIR/.barn/env and sync"; fi
if [ -n "${KIRO_API_KEY:-}" ]; then say pass "kiro token" "KIRO_API_KEY set"
elif [ -s ~/.local/share/kiro-cli/data.sqlite3 ]; then say pass "kiro token" "data.sqlite3"
else say warn "kiro token" "none; set KIRO_API_KEY in SYNCDIR/.barn/env and sync"; fi
if out=$(timeout 30 bash -lc 'claude auth status' 2>&1); then say pass "claude signed in" "$(echo "$out" | jq -r .authMethod 2>/dev/null)"
else say fail "claude signed in" "claude auth status: not logged in"; fi
if out=$(timeout 30 bash -lc 'kiro-cli whoami' 2>&1); then say pass "kiro signed in" "$(echo "$out" | head -1 | head -c 80)"
else say fail "kiro signed in" "$(echo "$out" | tail -1 | head -c 120)"; fi
"""

# Run with a tty: idle signal 3 needs SSH-over-SSM logins in logind with a tty.
TTY_CHECK = 'loginctl show-session "$XDG_SESSION_ID" -p Class -p TTY 2>&1 | tr "\\n" " "'


def simulate_checks(ctx):
    """Simulate the permissions the CLI needs on the developer's role (catches a
    silently removed policy), and two it must not have. docs/account-prerequisites.md
    lists them."""
    a = ctx.account
    role = a.developer_role_arn
    if not role:
        return [(WARN, "policy simulation", "skipped: no developer_role_arn in the account file")]
    _, part, _, _, acct, _ = role.split(":", 5)
    ec2 = f"arn:{part}:ec2:{a.region}:{acct}"
    ssm = f"arn:{part}:ssm:{a.region}"
    ami_acct = "" if a.ami_ssm_parameter.startswith("/aws/service/") else acct

    def ctx_entry(key, value, kind="string"):
        return {"ContextKeyName": key, "ContextKeyValues": [value], "ContextKeyType": kind}

    base = [
        ctx_entry("aws:ResourceTag/barn", "true"),
        ctx_entry("aws:RequestTag/barn", "true"),
        ctx_entry("ssm:resourceTag/barn", "true"),
        ctx_entry("ssm:SessionDocumentAccessCheck", "true", "boolean"),
        ctx_entry("s3:prefix", "env/x"),
        # Without it, region-restricting SCPs make the simulator report explicitDeny.
        ctx_entry("aws:RequestedRegion", a.region),
    ]
    on_create = [ctx_entry("ec2:CreateAction", "RunInstances")]
    # action, resource, what, extra context
    needed = [
        ("ec2:RunInstances", f"{ec2}:instance/*", "tagged instances", []),
        ("ec2:CreateVolume", f"{ec2}:volume/*", "tagged volumes", []),
        ("ec2:CreateTags", f"{ec2}:instance/*", "new instances", on_create),
        ("ec2:StartInstances", f"{ec2}:instance/i-0", "tagged instances", []),
        ("ec2:StopInstances", f"{ec2}:instance/i-0", "tagged instances", []),
        ("ec2:TerminateInstances", f"{ec2}:instance/i-0", "tagged instances", []),
        ("ec2:ModifyInstanceAttribute", f"{ec2}:instance/i-0", "instance type", [ctx_entry("ec2:Attribute", "InstanceType")]),
        ("ec2:AttachVolume", f"{ec2}:volume/vol-0", "tagged volumes", []),
        ("ec2:GetConsoleOutput", f"{ec2}:instance/i-0", "tagged instances", []),
        ("ssm:GetParameter", f"{ssm}:{ami_acct}:parameter{a.ami_ssm_parameter}", "the AMI parameter", []),
        ("ssm:StartSession", f"{ec2}:instance/i-0", "tagged instances", []),
        ("ssm:StartSession", f"{ssm}::document/AWS-StartSSHSession", "the SSH document", []),
        ("s3:PutObject", f"arn:{part}:s3:::{a.state_bucket}/env/x", "state objects", []),
        ("s3:DeleteObject", f"arn:{part}:s3:::{a.state_bucket}/env/x", "state lockfiles", []),
        ("s3:ListBucket", f"arn:{part}:s3:::{a.state_bucket}", "the state bucket", []),
    ]
    # Recommended, not required: a role that can do these can get into anyone's box.
    forbidden = [
        ("ssm:StartSession", f"{ssm}:{acct}:document/SSM-SessionManagerRunShell", "policy denies shell sessions",
         "allowed; anyone with this role can open a shell on any box", []),
        ("ec2:ModifyInstanceAttribute", f"{ec2}:instance/i-0", "policy denies changing user data",
         "allowed; anyone with this role can run code as root on any box", [ctx_entry("ec2:Attribute", "UserData")]),
    ]
    iam = ctx.aws.client("iam", config=Config(retries={"mode": "standard", "max_attempts": 10}))

    def decision(action, resource, extra):
        return iam.simulate_principal_policy(
            PolicySourceArn=role, ActionNames=[action], ResourceArns=[resource], ContextEntries=base + extra
        )["EvaluationResults"][0]

    results = []
    for action, resource, what, extra in needed:
        r = decision(action, resource, extra)
        ok = r["EvalDecision"] == "allowed"
        label = f"policy allows {action} on {what}"
        if not ok and r.get("OrganizationsDecisionDetail", {}).get("AllowedByOrganizations") is False:
            # The simulator applies SCPs even where they do not bind (a management account).
            results.append((WARN, label, "denied by an Organizations SCP in the simulator"))
            continue
        results.append((PASS if ok else FAIL, label, "" if ok else r["EvalDecision"]))
    for action, resource, label, problem, extra in forbidden:
        denied = decision(action, resource, extra)["EvalDecision"] != "allowed"
        results.append((PASS if denied else WARN, label, "" if denied else problem))
    return results


def box_name(alias):
    """The hostname first boot gives the box (mirrors the instance root's local.hostname)."""
    return re.sub(r"[^a-z0-9-]+", "-", alias.lower()).strip("-")[:63]


def mnt_refs(ctx):
    hits = []
    for d in sources(ctx):
        for p in d.rglob("*"):
            if p == d / "README.md":
                continue  # not synced
            if p.is_file() and p.stat().st_size < 1_000_000:
                try:
                    if "/mnt/" in p.read_text(errors="strict"):
                        hits.append(str(p))
                except (UnicodeDecodeError, OSError):
                    pass
    return hits


def summarize(results):
    counts = {k: sum(1 for s, _, _ in results if s == k) for k in (PASS, WARN, FAIL)}
    parts = [f"[ok]{counts[PASS]} passed[/]"]
    if counts[WARN]:
        parts.append(f"[warn]{counts[WARN]} warning{'s' * (counts[WARN] != 1)}[/]")
    parts.append(f"[fail]{counts[FAIL]} failed[/]" if counts[FAIL] else "[muted]0 failed[/]")
    ui.err.print()
    ui.err.print(" [muted]·[/] ".join(parts))
    return 1 if counts[FAIL] else 0


def doctor(ctx, env):
    results = []

    def add(status, check, detail=""):
        results.append((status, check, detail))
        ui.check(status, check, detail)

    def section(title):
        ui.err.print(f"\n[name]{title}[/]")

    ui.header(ctx.alias(env), ctx.account.region, f"account {ctx.account.name}", f"profile {ctx.settings.aws_profile}")
    section("AWS")
    try:
        with ui.step("Checking credentials and permissions"):
            ident = ctx.aws.client("sts").get_caller_identity()
            try:
                checks = simulate_checks(ctx)
            except Exception as e:  # noqa: BLE001
                checks = [(FAIL, "policy simulation", str(e))]
            inst = ctx.find_instance(env)
    except Exception as e:  # noqa: BLE001 - report any credential problem as a failure
        add(FAIL, "aws credentials", str(e))
        return summarize(results)
    add(PASS, "aws credentials", ident["Arn"])
    for r in checks:
        add(*r)

    section("Instance")
    if not inst or inst["State"]["Name"] != "running":
        add(FAIL, "instance running", inst["State"]["Name"] if inst else "no instance")
        return summarize(results)
    add(PASS, "instance running", f"{inst['InstanceId']} {inst['InstanceType']}")
    iid = inst["InstanceId"]
    if ssm_online(ctx, iid):
        add(PASS, "ssm online")
    else:
        add(FAIL, "ssm online", "SSM does not report the instance online")
        return summarize(results)

    alias = ctx.alias(env)
    script = (
        REMOTE.replace("FAILED", FAILED)
        .replace("MARKER", MARKER)
        .replace("REGION", ctx.account.region)
        .replace("SYNCDIR", str(ctx.home / "home"))
        .replace("BOXNAME", box_name(ctx.alias(env)))
    )
    try:
        with ui.step("Running checks on the box"):
            r = ssh.run(alias, script, timeout=240)
            t = ssh.run(alias, TTY_CHECK, timeout=60, tty=True)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode not in (0, 1) or not r.stdout:
        add(FAIL, "ssh over ssm", (r.stderr.strip() if r else "timed out") or "no output")
        show_console(ctx, iid)
        return summarize(results)
    add(PASS, "ssh over ssm", alias)
    out = t.stdout.replace("\r", "").strip()
    has_tty = "TTY=pts/" in out and "Class=user" in out
    add(PASS if has_tty else FAIL, "ssh session in logind with a tty", out or t.stderr.strip())

    section("Box")
    for line in r.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0] in (PASS, WARN, FAIL):
            add(*parts)
            if parts[1] == "bootstrap marker" and parts[0] == FAIL:
                show_console(ctx, iid)

    section("Local")
    hits = mnt_refs(ctx)
    add(WARN if hits else PASS, "no /mnt/ paths in synced files", ", ".join(hits))

    return summarize(results)
