"""agent-platform command line (PLAN.md section 11)."""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import ssh
from .aws import Aws
from .config import BOX_USER, ConfigError, check_env_name, load_account, load_settings, repo_root
from .tf import Terraform, TerraformError

MARKER = "/var/lib/agent-platform/bootstrap-done"
FAILED = "/var/lib/agent-platform/bootstrap-failed"

# Rough us-east-1 on-demand $/hour, for `list`. Unknown types show "?".
HOURLY = {
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "m7i.large": 0.1008,
    "m7i.xlarge": 0.2016,
    "m7i.2xlarge": 0.4032,
    "m7i.4xlarge": 0.8064,
}
GP3_MONTH_PER_GB = 0.08


class Context:
    def __init__(self):
        self.repo = repo_root()
        self.settings = load_settings(self.repo)
        self.account = load_account(self.repo, self.settings.account)
        self.aws = Aws(self.account, self.settings)

    def alias(self, env):
        return f"{self.settings.user}-{env}"

    def tf(self, env, root):
        return Terraform(self.repo, self.aws, self.settings.user, env, root)

    def find_instance(self, env):
        """The environment's instance (not terminated), or None."""
        resp = self.aws.client("ec2").describe_instances(
            Filters=[
                {"Name": "tag:agent-platform", "Values": ["true"]},
                {"Name": "tag:agent-platform:owner", "Values": [self.settings.user]},
                {"Name": "tag:agent-platform:env", "Values": [env]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            ]
        )
        for r in resp["Reservations"]:
            for i in r["Instances"]:
                return i
        return None

    def require_instance(self, env):
        inst = self.find_instance(env)
        if not inst:
            raise ConfigError(f"no instance for environment {env!r}; run `agent-platform create {env}`")
        return inst


def log(msg):
    print(f"agent-platform: {msg}", file=sys.stderr, flush=True)


def wait_until(what, predicate, timeout, interval=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


def console_tail(ctx, instance_id, lines=40):
    out = ctx.aws.client("ec2").get_console_output(InstanceId=instance_id, Latest=True).get("Output", "")
    return "\n".join(out.splitlines()[-lines:]) or "(console output empty)"


def volume_vars(ctx, env, subnet_id):
    return {
        "region": ctx.account.region,
        "user": ctx.settings.user,
        "env": env,
        "subnet_id": subnet_id,
        "size_gb": ctx.settings.volume_gb,
        "kms_key_id": ctx.account.kms_key_id,
        "extra_tags": ctx.account.extra_tags,
    }


def instance_vars(ctx, env, vol, instance_type):
    s, a = ctx.settings, ctx.account
    return {
        "region": a.region,
        "user": s.user,
        "env": env,
        "ssh_public_key": s.public_key_path.read_text().strip(),
        "passwordless_sudo": s.passwordless_sudo,
        "idle_hours": s.idle_hours,
        "instance_type": instance_type,
        "volume_id": vol["volume_id"],
        "subnet_id": vol["subnet_id"],
        "security_group_id": a.security_group_id,
        "execution_instance_profile": a.execution_instance_profile,
        "ami_ssm_parameter": a.ami_ssm_parameter,
        "kms_key_id": a.kms_key_id,
        "extra_tags": a.extra_tags,
    }


def write_ssh_entry(ctx, env, instance_id):
    s, a = ctx.settings, ctx.account
    if a.role_mode == "create":
        ssh.write_aws_profile(
            ctx.aws.ssh_profile, a.developer_role_arn, s.aws_profile, a.region, f"agent-platform-{s.user}"
        )
    entry = ssh.host_entry(ctx.alias(env), instance_id, BOX_USER, s.private_key_path, ctx.aws.ssh_profile, a.region)
    ssh.write_host(ctx.alias(env), entry)


def ssm_online(ctx, instance_id):
    info = ctx.aws.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )["InstanceInformationList"]
    return bool(info) and info[0].get("PingStatus") == "Online"


def wait_for_bootstrap(ctx, env, instance_id, timeout=1500):
    alias = ctx.alias(env)
    log("waiting for SSM to report the instance online")
    wait_until("SSM online", lambda: ssm_online(ctx, instance_id), timeout=600)
    log("waiting for the bootstrap marker (first boot installs packages and tools)")

    def marker():
        try:
            r = ssh.run(alias, f"cat {MARKER} 2>/dev/null && echo OK; cat {FAILED} 2>/dev/null && echo FAILED; true", 90)
        except subprocess.TimeoutExpired:
            return None
        if "FAILED" in r.stdout.splitlines():
            raise RuntimeError(f"bootstrap failed: {r.stdout.strip()}")
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip().endswith("OK") else None

    try:
        out = wait_until("bootstrap marker", marker, timeout=timeout, interval=20)
    except (TimeoutError, RuntimeError):
        log("bootstrap did not finish; console output tail:")
        print(console_tail(ctx, instance_id), file=sys.stderr)
        raise
    for line in out.splitlines():
        if line.startswith("warning:"):
            log(f"bootstrap {line}")


def cmd_create(ctx, args):
    env = check_env_name(args.env)
    vol_tf = ctx.tf(env, "volume")
    inst_tf = ctx.tf(env, "instance")

    vol = vol_tf.outputs()
    subnet = vol.get("subnet_id") or ctx.account.subnet_ids[0]
    log(f"applying volume root for {ctx.alias(env)}")
    vol_tf.apply(volume_vars(ctx, env, subnet))
    vol = vol_tf.outputs()

    current = ctx.find_instance(env)
    size = args.size or (current or {}).get("InstanceType") or ctx.settings.default_size
    log(f"applying instance root ({size})")
    inst_tf.apply(instance_vars(ctx, env, vol, size))
    instance_id = inst_tf.outputs()["instance_id"]

    write_ssh_entry(ctx, env, instance_id)
    inst = ctx.find_instance(env)
    if inst and inst["State"]["Name"] in ("stopped", "stopping"):
        start_instance(ctx, instance_id)
    wait_for_bootstrap(ctx, env, instance_id)
    log(f"ready: `agent-platform ssh {env}` (or `ssh {ctx.alias(env)}`)")


def cmd_destroy(ctx, args):
    env = check_env_name(args.env)
    vol_tf = ctx.tf(env, "volume")
    vol = vol_tf.outputs()
    if args.volume:
        if not vol:
            log("no volume state for this environment")
        else:
            print(f"This permanently deletes the home volume {vol['volume_id']} of {ctx.alias(env)}.")
            typed = input(f"Type the environment name ({env}) to confirm: ")
            if typed.strip() != env:
                log("not confirmed; nothing destroyed")
                return 1
    inst_tf = ctx.tf(env, "instance")
    if vol:
        log("destroying instance root")
        size = (ctx.find_instance(env) or {}).get("InstanceType") or ctx.settings.default_size
        inst_tf.destroy(instance_vars(ctx, env, vol, size))
    ssh.write_host(ctx.alias(env), None)
    if args.volume and vol:
        log("destroying volume root")
        vol_tf.destroy(volume_vars(ctx, env, vol["subnet_id"]))
        ssh.forget_host_key(ctx.alias(env))
    log("done")
    return 0


def start_instance(ctx, instance_id):
    ec2 = ctx.aws.client("ec2")
    ec2.start_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])


def cmd_start(ctx, args):
    inst = ctx.require_instance(args.env)
    log(f"starting {inst['InstanceId']}")
    start_instance(ctx, inst["InstanceId"])
    write_ssh_entry(ctx, args.env, inst["InstanceId"])
    wait_until("SSM online", lambda: ssm_online(ctx, inst["InstanceId"]), timeout=600)
    log("running")


def cmd_stop(ctx, args):
    inst = ctx.require_instance(args.env)
    ec2 = ctx.aws.client("ec2")
    log(f"stopping {inst['InstanceId']} (home volume is kept)")
    ec2.stop_instances(InstanceIds=[inst["InstanceId"]])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[inst["InstanceId"]])
    log("stopped")


def cmd_list(ctx, args):
    ec2 = ctx.aws.client("ec2")
    owner = [
        {"Name": "tag:agent-platform", "Values": ["true"]},
        {"Name": "tag:agent-platform:owner", "Values": [ctx.settings.user]},
    ]
    envs = {}
    for v in ec2.describe_volumes(Filters=owner)["Volumes"]:
        tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
        # Root volumes carry the same tags; the home volume is the one at /dev/sdf.
        attached = [a["Device"] for a in v.get("Attachments", [])]
        if attached and "/dev/sdf" not in attached:
            continue
        envs.setdefault(tags.get("agent-platform:env"), {})["volume_gb"] = v["Size"]
    instances = ec2.describe_instances(
        Filters=owner + [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    )
    now = datetime.now(timezone.utc)
    for r in instances["Reservations"]:
        for i in r["Instances"]:
            tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
            e = envs.setdefault(tags.get("agent-platform:env"), {})
            e.update(id=i["InstanceId"], state=i["State"]["Name"], type=i["InstanceType"])
            if e["state"] == "running":
                e["uptime"] = now - i["LaunchTime"]
    print(f"{'ENV':<16} {'INSTANCE':<21} {'STATE':<9} {'TYPE':<12} {'UPTIME':<10} COST")
    for env in sorted(k for k in envs if k):
        e = envs[env]
        up = e.get("uptime")
        uptime = f"{int(up.total_seconds() // 3600)}h{int(up.total_seconds() % 3600 // 60):02d}m" if up else "-"
        hourly = HOURLY.get(e.get("type"))
        cost = []
        if e.get("state") == "running":
            cost.append(f"~${hourly:.3f}/h" if hourly else "?/h")
        if e.get("volume_gb"):
            cost.append(f"+ ~${e['volume_gb'] * GP3_MONTH_PER_GB:.0f}/mo volume")
        print(
            f"{env:<16} {e.get('id', '-'):<21} {e.get('state', 'no box'):<9} {e.get('type', '-'):<12} "
            f"{uptime:<10} {' '.join(cost)}"
        )


def cmd_ssh(ctx, args):
    check_env_name(args.env)
    os.execvp("ssh", ["ssh", ctx.alias(args.env), *args.ssh_args])


DURATION = re.compile(r"^(\d+)([mhd])$")


def parse_duration(text):
    m = DURATION.match(text.strip())
    if not m:
        raise ConfigError(f"bad duration {text!r}; use e.g. 30m, 8h, 2d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def cmd_hold(ctx, args):
    seconds = parse_duration(args.duration)
    r = ssh.run(
        ctx.alias(args.env),
        f'mkdir -p ~/.agent-platform && echo $(( $(date +%s) + {seconds} )) > ~/.agent-platform/hold-until '
        "&& date -d @$(cat ~/.agent-platform/hold-until)",
    )
    if r.returncode != 0:
        raise ConfigError(f"hold failed: {r.stderr.strip()}")
    log(f"held until {r.stdout.strip()}")


def cmd_sync(ctx, args):
    from .sync import sync

    return sync(ctx, args.env)


def cmd_doctor(ctx, args):
    from .doctor import doctor

    return doctor(ctx, args.env)


def build_parser():
    p = argparse.ArgumentParser(prog="agent-platform", description="Personal EC2 dev boxes for agent runs.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create", help="create or update an environment (also resizes)")
    c.add_argument("env")
    c.add_argument("--size", help="EC2 instance type")
    c.set_defaults(func=cmd_create)

    d = sub.add_parser("destroy", help="destroy the instance; --volume also deletes the home volume")
    d.add_argument("env")
    d.add_argument("--volume", action="store_true")
    d.set_defaults(func=cmd_destroy)

    for name, func in (("start", cmd_start), ("stop", cmd_stop), ("sync", cmd_sync), ("doctor", cmd_doctor)):
        s = sub.add_parser(name)
        s.add_argument("env")
        s.set_defaults(func=func)

    sub.add_parser("list", help="this developer's environments").set_defaults(func=cmd_list)

    s = sub.add_parser("ssh")
    s.add_argument("env")
    s.add_argument("ssh_args", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_ssh)

    h = sub.add_parser("hold", help="keep the box up for a duration (e.g. 8h)")
    h.add_argument("env")
    h.add_argument("duration")
    h.set_defaults(func=cmd_hold)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(Context(), args) or 0
    except (ConfigError, TerraformError, TimeoutError, RuntimeError) as e:
        log(f"error: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
