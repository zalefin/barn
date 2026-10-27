"""Terraform wrapper: one data dir per environment and root, state in S3."""

import json
import os
import subprocess


class TerraformError(Exception):
    pass


class Terraform:
    def __init__(self, repo, aws, user, env, root):
        self.repo = repo
        self.aws = aws
        self.user = user
        self.env = env
        self.root = root
        self.dir = repo / "terraform" / root
        self.data_dir = repo / ".state" / f"{user}-{env}" / root
        self._initialized = False

    def _environ(self):
        env = dict(os.environ)
        env.pop("AWS_PROFILE", None)
        env.update(self.aws.env())
        if not env.get("AWS_PROFILE"):
            env.pop("AWS_PROFILE", None)
        env["TF_DATA_DIR"] = str(self.data_dir)
        env["TF_IN_AUTOMATION"] = "1"
        cache = self.repo / ".state" / "plugin-cache"
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
