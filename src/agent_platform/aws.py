"""AWS sessions. In create role mode the developer role is assumed per command
(chained role sessions are capped at 1 hour)."""

import boto3


class Aws:
    def __init__(self, account, settings):
        self.account = account
        self.settings = settings
        self.base = boto3.Session(profile_name=settings.aws_profile, region_name=account.region)
        self._session = None
        self._creds = None

    @property
    def session(self):
        if self._session is None:
            if self.account.role_mode == "create":
                resp = self.base.client("sts").assume_role(
                    RoleArn=self.account.developer_role_arn,
                    RoleSessionName=f"agent-platform-{self.settings.user}",
                    DurationSeconds=3600,
                )
                c = resp["Credentials"]
                self._creds = c
                self._session = boto3.Session(
                    aws_access_key_id=c["AccessKeyId"],
                    aws_secret_access_key=c["SecretAccessKey"],
                    aws_session_token=c["SessionToken"],
                    region_name=self.account.region,
                )
            else:
                self._session = self.base
        return self._session

    def client(self, name):
        return self.session.client(name)

    def env(self):
        """Environment variables that give a subprocess (terraform) the same identity."""
        env = {"AWS_REGION": self.account.region, "AWS_DEFAULT_REGION": self.account.region}
        if self.account.role_mode == "create":
            self.session  # noqa: B018 - assumes the role
            c = self._creds
            env.update(
                AWS_ACCESS_KEY_ID=c["AccessKeyId"],
                AWS_SECRET_ACCESS_KEY=c["SecretAccessKey"],
                AWS_SESSION_TOKEN=c["SessionToken"],
                AWS_PROFILE="",
            )
        else:
            env["AWS_PROFILE"] = self.settings.aws_profile
        return env

    @property
    def ssh_profile(self):
        """AWS CLI profile the SSH ProxyCommand uses."""
        if self.account.role_mode == "create":
            # Per user too, so two developers can share a laptop (and the tests can).
            return f"agent-platform-{self.account.name}-{self.settings.user}"
        return self.settings.aws_profile
