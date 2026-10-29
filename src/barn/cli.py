"""barn command line (PLAN.md section 11)."""

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from importlib.metadata import version
from typing import Annotated

import typer
from rich import box
from rich.prompt import Prompt
from rich.table import Table

from . import ssh, ui
from .aws import Aws
from .config import BOX_USER, ConfigError, app_home, check_env_name, load_account, load_settings
from .tf import Terraform, TerraformError, install_roots

MARKER = "/var/lib/barn/bootstrap-done"
FAILED = "/var/lib/barn/bootstrap-failed"

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
STATE_STYLE = {"running": "ok", "stopped": "muted", "pending": "warn", "stopping": "warn"}


class Context:
    def __init__(self, verbose=False):
        self.home = app_home()
        self.settings = load_settings(self.home)
        self.account = load_account(self.home, self.settings.account)
        self.aws = Aws(self.account, self.settings)
        self.verbose = verbose
        self._roots_installed = False

    def alias(self, env):
        return f"{self.settings.user}-{env}"

    def tf(self, env, root):
        if not self._roots_installed:
            install_roots(self.home)
            self._roots_installed = True
        return Terraform(self.home, self.aws, self.settings.user, env, root, verbose=self.verbose)

    def find_instance(self, env):
        """The environment's instance (not terminated), or None."""
        resp = self.aws.client("ec2").describe_instances(
            Filters=[
                {"Name": "tag:barn", "Values": ["true"]},
                {"Name": "tag:barn:owner", "Values": [self.settings.user]},
                {"Name": "tag:barn:env", "Values": [env]},
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
            raise ConfigError(f"no instance for environment {env!r}; run `barn create {env}`")
        return inst


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


def show_console(ctx, instance_id):
    ui.block(console_tail(ctx, instance_id), title="console output (last 40 lines)")


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
    s = ctx.settings
    entry = ssh.host_entry(ctx.alias(env), instance_id, BOX_USER, s.private_key_path, s.aws_profile, ctx.account.region)
    ssh.write_host(ctx.alias(env), entry)


def ssm_online(ctx, instance_id):
    info = ctx.aws.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )["InstanceInformationList"]
    return bool(info) and info[0].get("PingStatus") == "Online"


def wait_online(ctx, instance_id):
    with ui.step("Online in SSM"):
        wait_until("SSM online", lambda: ssm_online(ctx, instance_id), timeout=600)


def wait_for_bootstrap(ctx, env, instance_id, timeout=1500):
    alias = ctx.alias(env)
    wait_online(ctx, instance_id)

    def marker():
        try:
            r = ssh.run(alias, f"cat {MARKER} 2>/dev/null && echo OK; cat {FAILED} 2>/dev/null && echo FAILED; true", 90)
        except subprocess.TimeoutExpired:
            return None
        if "FAILED" in r.stdout.splitlines():
            raise RuntimeError(f"bootstrap failed: {r.stdout.strip()}")
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip().endswith("OK") else None

    try:
        with ui.step("First boot") as s:
            s.update("installing packages and tools")
            out = wait_until("bootstrap marker", marker, timeout=timeout, interval=20)
    except (TimeoutError, RuntimeError):
        show_console(ctx, instance_id)
        raise
    for line in out.splitlines():
        if line.startswith("warning:"):
            ui.warn("bootstrap", line.removeprefix("warning:").strip())


def apply(tf, variables, s):
    s.result = tf.apply(variables, on_line=s.update)


def create(ctx, env, size=None):
    env = check_env_name(env)
    vol_tf = ctx.tf(env, "volume")
    inst_tf = ctx.tf(env, "instance")

    with ui.step("Reading state"):
        vol = vol_tf.outputs()
        current = ctx.find_instance(env)
    size = size or (current or {}).get("InstanceType") or ctx.settings.default_size
    ui.header(ctx.alias(env), size, ctx.account.region, f"account {ctx.account.name}")

    subnet = vol.get("subnet_id") or ctx.account.subnet_ids[0]
    existed = bool(vol)
    with ui.step("Home volume") as s:
        apply(vol_tf, volume_vars(ctx, env, subnet), s)
        changed = existed and " 0 changed" not in f" {s.result}"
        vol = vol_tf.outputs()
        s.result = " · ".join(p for p in (vol["volume_id"], vol["availability_zone"], s.result) if p)

    with ui.step("Instance") as s:
        apply(inst_tf, instance_vars(ctx, env, vol, size), s)
        instance_id = inst_tf.outputs()["instance_id"]
        s.result = " · ".join(p for p in (instance_id, s.result) if p)

    write_ssh_entry(ctx, env, instance_id)
    inst = ctx.find_instance(env)
    if inst and inst["State"]["Name"] in ("stopped", "stopping"):
        with ui.step("Starting"):
            start_instance(ctx, instance_id)
    wait_for_bootstrap(ctx, env, instance_id)
    if changed:
        grow_home(ctx, env)
    ready(ctx, env)


def grow_home(ctx, env):
    """Grow the home filesystem to a resized volume (first boot does it for new boxes)."""
    with ui.step("Growing home filesystem") as s:
        r = ssh.run(ctx.alias(env), 'sudo -n resize2fs "$(findmnt -n -o SOURCE ~)" 2>&1 && df -h ~ | tail -1')
        if r.returncode != 0:
            raise RuntimeError(f"resize2fs failed (needs passwordless sudo): {r.stdout.strip()}")
        size, _, free = r.stdout.split()[-5:-2]  # df: Filesystem Size Used Avail Use% Mounted
        s.result = f"{size} total, {free} free"


def ready(ctx, env):
    ui.err.print()
    ui.err.print(
        f"[ok]●[/] [name]{ctx.alias(env)}[/] is ready   "
        f"[accent]barn ssh {env}[/]  [muted]or[/]  [accent]ssh {ctx.alias(env)}[/]"
    )


def destroy(ctx, env, volume=False):
    env = check_env_name(env)
    vol_tf = ctx.tf(env, "volume")
    with ui.step("Reading state"):
        vol = vol_tf.outputs()
    if volume:
        if not vol:
            ui.warn("no volume state for this environment")
        else:
            ui.err.print(
                f"[fail]This permanently deletes the home volume[/] [name]{vol['volume_id']}[/] "
                f"[fail]of[/] [name]{ctx.alias(env)}[/][fail]: every file, tool and Docker image on it.[/]"
            )
            typed = Prompt.ask(f"Type [name]{env}[/] to confirm", console=ui.err, default="", show_default=False)
            if typed.strip() != env:
                ui.info("not confirmed; nothing destroyed")
                return 1
    inst_tf = ctx.tf(env, "instance")
    if vol:
        size = (ctx.find_instance(env) or {}).get("InstanceType") or ctx.settings.default_size
        with ui.step("Instance") as s:
            s.result = inst_tf.destroy(instance_vars(ctx, env, vol, size), on_line=s.update)
    ssh.write_host(ctx.alias(env), None)
    if volume and vol:
        with ui.step("Home volume") as s:
            s.result = vol_tf.destroy(volume_vars(ctx, env, vol["subnet_id"]), on_line=s.update)
        ssh.forget_host_key(ctx.alias(env))
    elif vol:
        ui.info("home volume kept", f"{vol['volume_id']}; `barn create {env}` brings the box back")
    return 0


def start_instance(ctx, instance_id):
    ec2 = ctx.aws.client("ec2")
    ec2.start_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])


def start(ctx, env):
    inst = ctx.require_instance(env)
    with ui.step(f"Starting {ctx.alias(env)}") as s:
        s.result = inst["InstanceId"]
        start_instance(ctx, inst["InstanceId"])
    write_ssh_entry(ctx, env, inst["InstanceId"])
    wait_online(ctx, inst["InstanceId"])
    ready(ctx, env)


def stop(ctx, env):
    inst = ctx.require_instance(env)
    ec2 = ctx.aws.client("ec2")
    with ui.step(f"Stopping {ctx.alias(env)}") as s:
        s.result = f"{inst['InstanceId']} · home volume kept"
        ec2.stop_instances(InstanceIds=[inst["InstanceId"]])
        ec2.get_waiter("instance_stopped").wait(InstanceIds=[inst["InstanceId"]])


def environments(ctx):
    """{env: {volume_gb, id, state, type, uptime}} for this developer."""
    ec2 = ctx.aws.client("ec2")
    owner = [
        {"Name": "tag:barn", "Values": ["true"]},
        {"Name": "tag:barn:owner", "Values": [ctx.settings.user]},
    ]
    envs = {}
    for v in ec2.describe_volumes(Filters=owner)["Volumes"]:
        tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
        # Root volumes carry the same tags; the home volume is the one at /dev/sdf.
        attached = [a["Device"] for a in v.get("Attachments", [])]
        if attached and "/dev/sdf" not in attached:
            continue
        envs.setdefault(tags.get("barn:env"), {})["volume_gb"] = v["Size"]
    instances = ec2.describe_instances(
        Filters=owner + [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    )
    now = datetime.now(timezone.utc)
    for r in instances["Reservations"]:
        for i in r["Instances"]:
            tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
            e = envs.setdefault(tags.get("barn:env"), {})
            e.update(id=i["InstanceId"], state=i["State"]["Name"], type=i["InstanceType"])
            if e["state"] == "running":
                e["uptime"] = now - i["LaunchTime"]
    return {k: v for k, v in envs.items() if k}


def env_table(envs, width=120):
    table = Table(box=box.SIMPLE_HEAD, header_style="muted", pad_edge=False)
    # The instance ID is the first thing to go in a narrow terminal.
    columns = ("ENV", "STATE", "TYPE", "UPTIME", "COST") + (("INSTANCE",) if width >= 90 else ())
    for col in columns:
        table.add_column(col, no_wrap=True, justify="right" if col in ("UPTIME", "COST") else "left")
    hourly_total = 0.0
    for env in sorted(envs):
        e = envs[env]
        state = e.get("state", "no box")
        up = e.get("uptime")
        hourly = HOURLY.get(e.get("type"))
        cost = []
        if state == "running":
            cost.append(f"${hourly:.2f}/h" if hourly else "?/h")
            hourly_total += hourly or 0
        if e.get("volume_gb"):
            cost.append(f"${e['volume_gb'] * GP3_MONTH_PER_GB:.0f}/mo")
        row = [
            f"[name]{env}[/]",
            f"[{STATE_STYLE.get(state, 'muted')}]{state}[/]",
            e.get("type", "[muted]-[/]"),
            ui.elapsed(up.total_seconds()) if up else "[muted]-[/]",
            " + ".join(cost) or "[muted]-[/]",
            f"[muted]{e.get('id', '-')}[/]",
        ]
        table.add_row(*row[: len(columns)])
    running = sum(1 for e in envs.values() if e.get("state") == "running")
    table.caption = f"{len(envs)} environment{'s' * (len(envs) != 1)} · {running} running" + (
        f" · ~${hourly_total:.2f}/h" if hourly_total else ""
    )
    table.caption_style = "muted"
    table.caption_justify = "left"
    return table


def list_envs(ctx):
    envs = environments(ctx)
    if not envs:
        ui.info("no environments", "create one with `barn create <env>`")
        return
    ui.out.print(env_table(envs, ui.out.width))


DURATION = re.compile(r"^(\d+)([mhd])$")


def parse_duration(text):
    m = DURATION.match(text.strip())
    if not m:
        raise ConfigError(f"bad duration {text!r}; use e.g. 30m, 8h, 2d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def hold(ctx, env, duration):
    seconds = parse_duration(duration)
    with ui.step(f"Holding {ctx.alias(env)}") as s:
        r = ssh.run(
            ctx.alias(env),
            f'mkdir -p ~/.barn && echo $(( $(date +%s) + {seconds} )) > ~/.barn/hold-until '
            "&& date -d @$(cat ~/.barn/hold-until)",
        )
        if r.returncode != 0:
            raise ConfigError(f"hold failed: {r.stderr.strip()}")
        s.result = f"no idle stop until {r.stdout.strip()}"


# --- command line ---

app = typer.Typer(
    name="barn",
    help="Personal EC2 dev boxes for long-running agent runs.",
    epilog=f"Settings, account files and terraform state live in {app_home()} (BARN_HOME).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_enable=False,
)

SETUP, ENVIRONMENTS, ON_THE_BOX = "Setup", "Environments", "On the box"
Env = Annotated[str, typer.Argument(metavar="ENV", help="Environment name, e.g. [cyan]dev[/] (the box is [cyan]<user>-<env>[/]).")]
Verbose = Annotated[bool, typer.Option("--verbose", "-v", help="Stream terraform output instead of a spinner.")]


def _version(value: bool):
    if value:
        ui.out.print(f"barn {version('barn')}")
        raise typer.Exit()


@app.callback()
def _main(
    _: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version and exit.")
    ] = False,
):
    pass


@app.command("init", rich_help_panel=SETUP)
def init_cmd(
    account_file: Annotated[
        str | None,
        typer.Option("--account-file", metavar="PATH", help="Copy this account file in (e.g. from your platform admin)."),
    ] = None,
    account: Annotated[
        str | None, typer.Option(metavar="NAME", help="Account name (default: the account file's name, or default).")
    ] = None,
    aws_profile: Annotated[
        str | None, typer.Option("--aws-profile", metavar="PROFILE", help="AWS CLI profile to use.")
    ] = None,
):
    """Create the barn config files, check your tools, and list what you still need to edit. Safe to re-run."""
    from .setup import init

    raise typer.Exit(init(app_home(), account, aws_profile, account_file))


@app.command("create", rich_help_panel=ENVIRONMENTS)
def create_cmd(
    env: Env,
    size: Annotated[str | None, typer.Option(metavar="TYPE", help="EC2 instance type (default: default_size).")] = None,
    verbose: Verbose = False,
):
    """Create or update an environment: home volume, then box. Re-run with --size to resize."""
    create(Context(verbose), env, size)


@app.command("destroy", rich_help_panel=ENVIRONMENTS)
def destroy_cmd(
    env: Env,
    volume: Annotated[bool, typer.Option("--volume", help="Also delete the home volume (asks first).")] = False,
    verbose: Verbose = False,
):
    """Delete the box and keep the home volume."""
    raise typer.Exit(destroy(Context(verbose), env, volume))


@app.command("start", rich_help_panel=ENVIRONMENTS)
def start_cmd(env: Env):
    """Start a stopped box."""
    start(Context(), env)


@app.command("stop", rich_help_panel=ENVIRONMENTS)
def stop_cmd(env: Env):
    """Stop the box. A stopped box costs only its volumes."""
    stop(Context(), env)


@app.command("list", rich_help_panel=ENVIRONMENTS)
def list_cmd():
    """Your environments: state, type, uptime, rough cost."""
    list_envs(Context())


@app.command(
    "ssh",
    rich_help_panel=ON_THE_BOX,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ssh_cmd(typer_ctx: typer.Context, env: Env):
    """SSH into the box through SSM. Extra arguments go to ssh, e.g. [cyan]-N -L 8080:localhost:8080[/]."""
    ctx = Context()
    check_env_name(env)
    os.execvp("ssh", ["ssh", ctx.alias(env), *typer_ctx.args])


@app.command("sync", rich_help_panel=ON_THE_BOX)
def sync_cmd(env: Env):
    """Copy your home overlay onto the box. Never deletes; refuses while agents run."""
    from .sync import sync

    raise typer.Exit(sync(Context(), env))


@app.command("doctor", rich_help_panel=ON_THE_BOX)
def doctor_cmd(env: Env):
    """Check AWS, permissions, SSM, first boot, tools, network and sign-in."""
    from .doctor import doctor

    raise typer.Exit(doctor(Context(), env))


@app.command("hold", rich_help_panel=ON_THE_BOX)
def hold_cmd(env: Env, duration: Annotated[str, typer.Argument(metavar="DURATION", help="How long: 30m, 8h, 2d, ...")]):
    """Keep the box from idle-stopping for a while."""
    hold(Context(), env, duration)


def main(argv=None):
    try:
        app(argv)
    except TerraformError as e:
        ui.fail(str(e))
        if e.tail:
            ui.block(e.tail)
        if e.log:
            ui.info("full log", str(e.log))
        return 1
    except (ConfigError, TimeoutError, RuntimeError) as e:
        ui.fail(str(e))
        return 1
