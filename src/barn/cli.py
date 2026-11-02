"""barn command line (docs/plans/PLAN.md section 11)."""

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from importlib.metadata import version
from typing import Annotated

import typer
from botocore.exceptions import ClientError
from rich import box
from rich.prompt import Prompt
from rich.table import Table

from . import ssh, ui
from .aws import Aws
from .config import BOX_USER, ConfigError, app_home, check_name, load_account, load_settings
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


LIVE = ["pending", "running", "stopping", "stopped"]


def tag_map(resource):
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


class Context:
    def __init__(self, verbose=False):
        self.home = app_home()
        self.settings = load_settings(self.home)
        self.account = load_account(self.home, self.settings.account)
        self.aws = Aws(self.account, self.settings)
        self.verbose = verbose
        self._roots_installed = False

    def tf(self, name, root):
        if not self._roots_installed:
            install_roots(self.home)
            self._roots_installed = True
        return Terraform(self.home, self.aws, name, root, verbose=self.verbose)

    def _pages(self, method, key, filters):
        ec2 = self.aws.client("ec2")
        for page in ec2.get_paginator(method).paginate(Filters=filters):
            yield from page[key]

    def instances(self, name=None):
        """barn instances that are not terminated: all of them, or one box's."""
        filters = [{"Name": "tag:barn", "Values": ["true"]}, {"Name": "instance-state-name", "Values": LIVE}]
        if name:
            filters.append({"Name": "tag:barn:name", "Values": [name]})
        for r in self._pages("describe_instances", "Reservations", filters):
            yield from r["Instances"]

    def volumes(self, name=None):
        """barn volumes (home and root): all of them, or one box's."""
        filters = [{"Name": "tag:barn", "Values": ["true"]}]
        if name:
            filters.append({"Name": "tag:barn:name", "Values": [name]})
        return self._pages("describe_volumes", "Volumes", filters)

    def find_instance(self, name):
        """The box's instance (not terminated), or None."""
        return next(iter(self.instances(name)), None)

    def require_instance(self, name):
        inst = self.find_instance(name)
        if not inst:
            raise ConfigError(f"no box named {name!r}; `barn list` shows every box in the account")
        return inst

    def owners(self, name):
        """barn:owner of every instance and volume named name."""
        found = [*self.instances(name), *self.volumes(name)]
        return {tag_map(r).get("barn:owner", "") for r in found}


def check_owner(ctx, name, doing, force=False, hint=""):
    """An accident guard, not a security boundary (any manager may manage any box):
    refuse when the box or volume was created by someone else."""
    others = sorted(o or "nobody" for o in ctx.owners(name) - {ctx.settings.user})
    if others and not force:
        raise ConfigError(f"refusing to {doing} {name}: it is owned by {', '.join(others)}" + (f"; {hint}" if hint else ""))


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
    try:
        tail = console_tail(ctx, instance_id)
    except ClientError as e:  # guests may not read console output
        ui.info("console output not available", e.response["Error"].get("Code", str(e)))
        return
    ui.block(tail, title="console output (last 40 lines)")


def volume_vars(ctx, name, subnet_id):
    return {
        "region": ctx.account.region,
        "user": ctx.settings.user,
        "name": name,
        "subnet_id": subnet_id,
        "size_gb": ctx.settings.volume_gb,
        "kms_key_id": ctx.account.kms_key_id,
        "extra_tags": ctx.account.extra_tags,
    }


def instance_vars(ctx, name, vol, instance_type):
    s, a = ctx.settings, ctx.account
    return {
        "region": a.region,
        "user": s.user,
        "name": name,
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


def write_ssh_entry(ctx, name, instance_id):
    """The box's ~/.ssh/barn.conf entry, with this person's key and AWS profile."""
    s = ctx.settings
    entry = ssh.host_entry(name, instance_id, BOX_USER, s.private_key_path, s.aws_profile, ctx.account.region)
    ssh.write_host(name, entry)


def ssm_online(ctx, instance_id):
    info = ctx.aws.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )["InstanceInformationList"]
    return bool(info) and info[0].get("PingStatus") == "Online"


def wait_online(ctx, instance_id):
    with ui.step("Online in SSM"):
        wait_until("SSM online", lambda: ssm_online(ctx, instance_id), timeout=600)


def wait_for_bootstrap(ctx, name, instance_id, timeout=1500):
    alias = name
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


def create(ctx, name, size=None):
    name = check_name(name)
    with ui.step("Reading state"):
        check_owner(ctx, name, "create", hint="pick another name")
        vol_tf = ctx.tf(name, "volume")
        inst_tf = ctx.tf(name, "instance")
        vol = vol_tf.outputs()
        current = ctx.find_instance(name)
    size = size or (current or {}).get("InstanceType") or ctx.settings.default_size
    ui.header(name, size, ctx.account.region, f"account {ctx.account.name}")

    subnet = vol.get("subnet_id") or ctx.account.subnet_ids[0]
    existed = bool(vol)
    with ui.step("Home volume") as s:
        apply(vol_tf, volume_vars(ctx, name, subnet), s)
        changed = existed and " 0 changed" not in f" {s.result}"
        vol = vol_tf.outputs()
        s.result = " · ".join(p for p in (vol["volume_id"], vol["availability_zone"], s.result) if p)

    with ui.step("Instance") as s:
        apply(inst_tf, instance_vars(ctx, name, vol, size), s)
        instance_id = inst_tf.outputs()["instance_id"]
        s.result = " · ".join(p for p in (instance_id, s.result) if p)

    write_ssh_entry(ctx, name, instance_id)
    inst = ctx.find_instance(name)
    if inst and inst["State"]["Name"] in ("stopped", "stopping"):
        with ui.step("Starting"):
            start_instance(ctx, instance_id)
    wait_for_bootstrap(ctx, name, instance_id)
    if changed:
        grow_home(ctx, name)
    push_guest_keys(ctx, name)
    ready(ctx, name)


def push_guest_keys(ctx, name):
    """Make the box's guests block in authorized_keys exactly guest_keys (an empty
    list revokes every guest). Keys outside the barn blocks are kept."""
    keys = ctx.settings.guest_keys
    with ui.step("Guest keys") as s:
        r = ssh.run(name, ssh.authorized_keys_script("guests", keys))
        if r.returncode != 0:
            raise RuntimeError(f"updating authorized_keys failed: {(r.stderr or r.stdout).strip()}")
        s.result = ", ".join(k.split()[2] if len(k.split()) > 2 else k.split()[0] for k in keys) or "none"


def grow_home(ctx, name):
    """Grow the home filesystem to a resized volume (first boot does it for new boxes)."""
    with ui.step("Growing home filesystem") as s:
        r = ssh.run(name, 'sudo -n resize2fs "$(findmnt -n -o SOURCE ~)" 2>&1 && df -h ~ | tail -1')
        if r.returncode != 0:
            raise RuntimeError(f"resize2fs failed (needs passwordless sudo): {r.stdout.strip()}")
        size, _, free = r.stdout.split()[-5:-2]  # df: Filesystem Size Used Avail Use% Mounted
        s.result = f"{size} total, {free} free"


def ready(ctx, name):
    ui.err.print()
    ui.err.print(f"[ok]●[/] [name]{name}[/] is ready   [accent]barn ssh {name}[/]  [muted]or[/]  [accent]ssh {name}[/]")


def destroy(ctx, name, volume=False, force=False):
    name = check_name(name)
    with ui.step("Reading state"):
        check_owner(ctx, name, "destroy", force, hint="pass --force to destroy it anyway")
        vol_tf = ctx.tf(name, "volume")
        vol = vol_tf.outputs()
    if volume:
        if not vol:
            ui.warn("no volume state for this box")
        else:
            ui.err.print(
                f"[fail]This permanently deletes the home volume[/] [name]{vol['volume_id']}[/] "
                f"[fail]of[/] [name]{name}[/][fail]: every file, tool and Docker image on it.[/]"
            )
            typed = Prompt.ask(f"Type [name]{name}[/] to confirm", console=ui.err, default="", show_default=False)
            if typed.strip() != name:
                ui.info("not confirmed; nothing destroyed")
                return 1
    inst_tf = ctx.tf(name, "instance")
    if vol:
        size = (ctx.find_instance(name) or {}).get("InstanceType") or ctx.settings.default_size
        with ui.step("Instance") as s:
            s.result = inst_tf.destroy(instance_vars(ctx, name, vol, size), on_line=s.update)
    ssh.write_host(name, None)
    if volume and vol:
        with ui.step("Home volume") as s:
            s.result = vol_tf.destroy(volume_vars(ctx, name, vol["subnet_id"]), on_line=s.update)
        ssh.forget_host_key(name)
    elif vol:
        ui.info("home volume kept", f"{vol['volume_id']}; `barn create {name}` brings the box back")
    return 0


def start_instance(ctx, instance_id):
    ec2 = ctx.aws.client("ec2")
    ec2.start_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])


def start(ctx, name):
    inst = ctx.require_instance(check_name(name))
    with ui.step(f"Starting {name}") as s:
        s.result = inst["InstanceId"]
        start_instance(ctx, inst["InstanceId"])
    write_ssh_entry(ctx, name, inst["InstanceId"])
    wait_online(ctx, inst["InstanceId"])
    ready(ctx, name)


def stop(ctx, name):
    inst = ctx.require_instance(check_name(name))
    ec2 = ctx.aws.client("ec2")
    with ui.step(f"Stopping {name}") as s:
        s.result = f"{inst['InstanceId']} · home volume kept"
        ec2.stop_instances(InstanceIds=[inst["InstanceId"]])
        ec2.get_waiter("instance_stopped").wait(InstanceIds=[inst["InstanceId"]])


def connect(ctx, name, quiet=False):
    """Point ~/.ssh/barn.conf's entry for the box at its current instance, with
    this person's key and profile. Returns the instance."""
    inst = ctx.require_instance(check_name(name))
    write_ssh_entry(ctx, name, inst["InstanceId"])
    state = inst["State"]["Name"]
    owner = tag_map(inst).get("barn:owner", "?")
    if not quiet:
        ui.done(f"{name} → {inst['InstanceId']}", f"owner {owner} · ~/.ssh/barn.conf")
    if state != "running":
        ui.warn(f"{name} is {state}", f"ask a manager to run `barn start {name}`" if state == "stopped" else "")
    elif not quiet:
        ui.err.print(
            f"[ok]●[/] [accent]ssh {name}[/]  [muted]·[/]  [accent]rsync -a ./dir {name}:~/[/]  [muted]·[/]  "
            f"[accent]herdr --remote {name}[/]"
        )
    return inst


def boxes(ctx):
    """{name: {owner, volume_gb, id, state, type, uptime}} for every barn box in
    the account. Resources without barn:name (made before box names) are skipped."""
    found = {}
    for v in ctx.volumes():
        tags = tag_map(v)
        # Root volumes carry the same tags; the home volume is the one at /dev/sdf.
        attached = [a["Device"] for a in v.get("Attachments", [])]
        if not tags.get("barn:name") or attached and "/dev/sdf" not in attached:
            continue
        b = found.setdefault(tags["barn:name"], {})
        b.update(volume_gb=v["Size"], owner=tags.get("barn:owner", ""))
    now = datetime.now(timezone.utc)
    for i in ctx.instances():
        tags = tag_map(i)
        if not tags.get("barn:name"):
            continue
        b = found.setdefault(tags["barn:name"], {})
        b.update(id=i["InstanceId"], state=i["State"]["Name"], type=i["InstanceType"])
        b.setdefault("owner", tags.get("barn:owner", ""))
        if b["state"] == "running":
            b["uptime"] = now - i["LaunchTime"]
    return found


def box_table(found, me, width=120):
    table = Table(box=box.SIMPLE_HEAD, header_style="muted", pad_edge=False)
    # The instance ID is the first thing to go in a narrow terminal.
    columns = ("NAME", "OWNER", "STATE", "TYPE", "UPTIME", "COST") + (("INSTANCE",) if width >= 100 else ())
    for col in columns:
        table.add_column(col, no_wrap=True, justify="right" if col in ("UPTIME", "COST") else "left")
    hourly_total = 0.0
    for name in sorted(found):
        b = found[name]
        state = b.get("state", "no box")
        up = b.get("uptime")
        hourly = HOURLY.get(b.get("type"))
        cost = []
        if state == "running":
            cost.append(f"${hourly:.2f}/h" if hourly else "?/h")
            hourly_total += hourly or 0
        if b.get("volume_gb"):
            cost.append(f"${b['volume_gb'] * GP3_MONTH_PER_GB:.0f}/mo")
        owner = b.get("owner") or "?"
        row = [
            f"[name]{name}[/]",
            f"[accent]{owner} (you)[/]" if owner == me else owner,
            f"[{STATE_STYLE.get(state, 'muted')}]{state}[/]",
            b.get("type", "[muted]-[/]"),
            ui.elapsed(up.total_seconds()) if up else "[muted]-[/]",
            " + ".join(cost) or "[muted]-[/]",
            f"[muted]{b.get('id', '-')}[/]",
        ]
        table.add_row(*row[: len(columns)])
    running = sum(1 for b in found.values() if b.get("state") == "running")
    mine = sum(1 for b in found.values() if b.get("owner") == me)
    table.caption = f"{len(found)} box{'es' * (len(found) != 1)} ({mine} yours) · {running} running" + (
        f" · ~${hourly_total:.2f}/h" if hourly_total else ""
    )
    table.caption_style = "muted"
    table.caption_justify = "left"
    return table


def list_boxes(ctx):
    found = boxes(ctx)
    if not found:
        ui.info("no boxes in this account", f"create one with `barn create {ctx.settings.user}-dev`")
        return
    ui.out.print(box_table(found, ctx.settings.user, ui.out.width))


DURATION = re.compile(r"^(\d+)([mhd])$")


def parse_duration(text):
    m = DURATION.match(text.strip())
    if not m:
        raise ConfigError(f"bad duration {text!r}; use e.g. 30m, 8h, 2d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def hold(ctx, name, duration):
    seconds = parse_duration(duration)
    with ui.step(f"Holding {check_name(name)}") as s:
        r = ssh.run(
            name,
            f'mkdir -p ~/.barn && echo $(( $(date +%s) + {seconds} )) > ~/.barn/hold-until '
            "&& date -d @$(cat ~/.barn/hold-until)",
        )
        if r.returncode != 0:
            raise ConfigError(f"hold failed: {r.stderr.strip()}")
        s.result = f"no idle stop until {r.stdout.strip()}"


# --- command line ---

app = typer.Typer(
    name="barn",
    help="EC2 dev boxes for long-running agent runs. Managers create them; anyone can connect.",
    epilog=f"Settings, account files and terraform state live in {app_home()} (BARN_HOME).",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_enable=False,
)

SETUP, MANAGE, CONNECT = "Setup", "Manage boxes (managers)", "Use a box (anyone)"
Name = Annotated[
    str, typer.Argument(metavar="NAME", help="The box's full name, e.g. [cyan]zach-dev[/]; also its SSH alias.")
]
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


@app.command("create", rich_help_panel=MANAGE)
def create_cmd(
    name: Name,
    size: Annotated[str | None, typer.Option(metavar="TYPE", help="EC2 instance type (default: default_size).")] = None,
    verbose: Verbose = False,
):
    """Create or update a box: home volume, then instance. Re-run with --size to resize."""
    create(Context(verbose), name, size)


@app.command("destroy", rich_help_panel=MANAGE)
def destroy_cmd(
    name: Name,
    volume: Annotated[bool, typer.Option("--volume", help="Also delete the home volume (asks first).")] = False,
    force: Annotated[bool, typer.Option("--force", help="Destroy a box someone else created.")] = False,
    verbose: Verbose = False,
):
    """Delete the box and keep the home volume."""
    raise typer.Exit(destroy(Context(verbose), name, volume, force))


@app.command("start", rich_help_panel=MANAGE)
def start_cmd(name: Name):
    """Start a stopped box."""
    start(Context(), name)


@app.command("stop", rich_help_panel=MANAGE)
def stop_cmd(name: Name):
    """Stop the box. A stopped box costs only its volumes."""
    stop(Context(), name)


@app.command("sync", rich_help_panel=MANAGE)
def sync_cmd(name: Name):
    """Push your guest_keys and home overlay onto your box. Never deletes files; skips them while agents run."""
    from .sync import sync

    raise typer.Exit(sync(Context(), name))


@app.command("list", rich_help_panel=CONNECT)
def list_cmd():
    """Every box in the account: owner, state, type, uptime, rough cost."""
    list_boxes(Context())


@app.command("connect", rich_help_panel=CONNECT)
def connect_cmd(name: Name):
    """Add the box to ~/.ssh/barn.conf, so plain ssh, scp, rsync and herdr --remote work."""
    connect(Context(), name)


@app.command(
    "ssh",
    rich_help_panel=CONNECT,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ssh_cmd(typer_ctx: typer.Context, name: Name):
    """SSH into the box through SSM. Extra arguments go to ssh, e.g. [cyan]-N -L 8080:localhost:8080[/]."""
    inst = connect(Context(), name, quiet=True)
    if inst["State"]["Name"] in ("stopped", "stopping"):
        raise typer.Exit(1)  # connect said so; a pending box is reachable in a moment
    os.execvp("ssh", ["ssh", name, *typer_ctx.args])


@app.command("doctor", rich_help_panel=CONNECT)
def doctor_cmd(name: Name):
    """Check AWS, permissions, SSM, first boot, tools, network and sign-in."""
    from .doctor import doctor

    raise typer.Exit(doctor(Context(), name))


@app.command("hold", rich_help_panel=CONNECT)
def hold_cmd(name: Name, duration: Annotated[str, typer.Argument(metavar="DURATION", help="How long: 30m, 8h, 2d, ...")]):
    """Keep the box from idle-stopping for a while."""
    hold(Context(), name, duration)


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
    except ClientError as e:
        err = e.response.get("Error", {})
        ui.fail(f"AWS {e.operation_name}: {err.get('Code', 'error')}", err.get("Message", ""))
        return 1
