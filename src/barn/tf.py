"""Terraform wrapper. The roots ship inside this package and are copied into the
barn home, so terraform always runs from the same place: one data dir
per environment and root, state in the account's S3 bucket."""

import json
import os
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).parent
# Package directories copied into the home; the instance root reads ../../bootstrap.
MANAGED = ("terraform", "bootstrap")


class TerraformError(Exception):
    pass


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
    def __init__(self, home, aws, user, env, root):
        self.home = home
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
            detail = (r.stderr or "").strip() if capture else ""
            raise TerraformError(f"terraform {args[0]} ({self.root}) failed {detail}".strip())
        return r.stdout

    def init(self):
        if self._initialized:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        acct = self.aws.account
        self._run(
            "init",
            "-input=false",
            "-reconfigure",
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

    def apply(self, variables):
        self.init()
        self._run("apply", "-input=false", "-auto-approve", self._var_file(variables))

    def destroy(self, variables):
        self.init()
        self._run("destroy", "-input=false", "-auto-approve", self._var_file(variables))

    def outputs(self):
        self.init()
        out = json.loads(self._run("output", "-json", capture=True) or "{}")
        return {k: v["value"] for k, v in out.items()}
