"""AWS sessions. Everything (boto3, terraform, the SSH ProxyCommand) uses the
person's own AWS CLI profile; if that profile assumes a role, the AWS SDKs
and CLI do it themselves."""

import boto3
from botocore.exceptions import ProfileNotFound

from .config import ConfigError


class Aws:
    def __init__(self, account, settings):
        self.account = account
        self.profile = settings.aws_profile
        try:
            self.session = boto3.Session(profile_name=self.profile, region_name=account.region)
        except ProfileNotFound as e:
            raise ConfigError(f"{e}; set aws_profile in config.toml to a profile in ~/.aws/config") from e

    def client(self, name, **kwargs):
        return self.session.client(name, **kwargs)

    def env(self):
        """Environment variables that give a subprocess (terraform) the same identity."""
        return {
            "AWS_PROFILE": self.profile,
            "AWS_REGION": self.account.region,
            "AWS_DEFAULT_REGION": self.account.region,
        }
