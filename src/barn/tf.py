"""Terraform wrapper. The roots ship inside this package and are copied into the
barn home, so terraform always runs from the same place: one data dir
per environment and root, state in the account's S3 bucket."""

import json
import os
import re
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).parent
# Package directories copied into the home; the instance root reads ../../bootstrap.
MANAGED = ("terraform", "bootstrap")


# "Apply complete! Resources: 1 added, 0 changed, 0 destroyed." / "Destroy complete! Resources: 2 destroyed."
SUMMARY = re.compile(r"(?:Apply|Destroy) complete! Resources: (.*)\.")
# Resource progress, e.g. "aws_instance.box: Still creating... [10s elapsed]"; not plan output.
PROGRESS = re.compile(r"^[\w.-]+(\[[^\]]*\])?: \S")


class TerraformError(Exception):
    def __init__(self, message, log=None, tail=""):
        super().__init__(message)
        self.log = log
        self.tail = tail


def summary(output):
    """`1 added, 0 changed, 0 destroyed` from apply/destroy output, or ""."""
    m = SUMMARY.search(output)
    return m.group(1) if m else ""


def install_roots(home):
    """Make home/terraform and home/bootstrap match the installed package. Files
    are replaced atomically and only when they differ, so a concurrent run is not
    disturbed; files the package no longer ships are removed."""
    for name in MANAGED:
        src, dst = PACKAGE / name, home / name
        wanted = set()
        for f in src.rglob("*"):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            rel = f.relative_to(src)
            wanted.add(rel)
            target = dst / rel
            data = f.read_bytes()
            if target.exists() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        for f in dst.rglob("*"):
            if f.is_file() and f.relative_to(dst) not in wanted:
                f.unlink()


class Terraform:
    def __init__(self, home, aws, user, env, root, verbose=False):
        self.home = home
        self.verbose = verbose
        self.aws = aws
        self.user = user
        self.env = env
        self.root = root
        self.dir = home / "terraform" / root
        self.data_dir = home / "state" / f"{user}-{env}" / root
        self._initialized = False

    def _environ(self):
        env = dict(os.environ)
        env.update(self.aws.env())
        env["TF_DATA_DIR"] = str(self.data_dir)
        env["TF_IN_AUTOMATION"] = "1"
        cache = self.home / "state" / "plugin-cache"
        cache.mkdir(parents=True, exist_ok=True)
        env.setdefault("TF_PLUGIN_CACHE_DIR", str(cache))
        return env

    def _run(self, *args, capture=False):
        cmd = ["terraform", f"-chdir={self.dir}", *args]
        r = subprocess.run(cmd, env=self._environ(), text=True, capture_output=capture)
        if r.returncode != 0:
            raise TerraformError(f"terraform {args[0]} ({self.root}) failed", tail=(r.stderr or "") if capture else "")
        return r.stdout

    def _run_logged(self, *args, on_line=None):
        """Run apply/destroy with its output in <data dir>/<command>.log (or on
        the terminal when verbose). Returns the result summary."""
        if self.verbose:
            self._run(*args)
            return ""
        log = self.data_dir / f"{args[0]}.log"
        cmd = ["terraform", f"-chdir={self.dir}", *args, "-no-color"]
        lines = []
        with open(log, "w") as f, subprocess.Popen(
            cmd, env=self._environ(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as p:
            for line in p.stdout:
                f.write(line)
                lines.append(line)
                if on_line and PROGRESS.match(line):
                    on_line(line.strip())
        if p.returncode != 0:
            tail = "".join(lines[-25:])
            raise TerraformError(f"terraform {args[0]} ({self.root}) failed", log=log, tail=tail)
        return summary("".join(lines))

    def init(self):
        if self._initialized:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        acct = self.aws.account
        self._run(
            "init",
            "-input=false",
            "-reconfigure",
            "-no-color",
            f"-backend-config=bucket={acct.state_bucket}",
            f"-backend-config=key=env/{self.user}/{self.env}/{self.root}.tfstate",
            f"-backend-config=region={acct.region}",
            "-backend-config=use_lockfile=true",
            # Keeps any workspace listing under env/, where developers may list.
            "-backend-config=workspace_key_prefix=env/workspaces",
            capture=True,
        )
        self._initialized = True

    def _var_file(self, variables):
        path = self.data_dir / "vars.tfvars.json"
        path.write_text(json.dumps(variables, indent=2))
        return f"-var-file={path}"

    def apply(self, variables, on_line=None):
        self.init()
        return self._run_logged("apply", "-input=false", "-auto-approve", self._var_file(variables), on_line=on_line)

    def destroy(self, variables, on_line=None):
        self.init()
        return self._run_logged("destroy", "-input=false", "-auto-approve", self._var_file(variables), on_line=on_line)

    def outputs(self):
        self.init()
        out = json.loads(self._run("output", "-json", "-no-color", capture=True) or "{}")
        return {k: v["value"] for k, v in out.items()}
