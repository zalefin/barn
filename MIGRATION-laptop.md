# Migration: move the barn setup to the user's laptop

Written 2026-11-02 on the devbox; not done yet. Read PLAN.md first (sections 0,
1 and 23).

For the next agent, running on the user's laptop (WSL). PLAN.md section 1's rules
apply: no commits unless asked, confirm every AWS change with the user, never
print or write secrets into tracked files.

## 1. Where things stand

- **Code:** the section 23 work is pushed to `main` on
  `git@git.example.com:zach/barn.git` (9a50624) and
  `.../barn-platform-minimal.git` (1500bea). Pull both on the laptop first;
  `barn --help` must list `connect`.
- **Sandbox account 123456789012, us-east-1:**
  - IAM roles `barn-manager` and `barn-guest`.
  - IAM users: `zach` (admin; trusted by both roles), and the test users
    `alice` and `foo`. `barn-dev` and `barn-ssm` are deleted.
  - One box, **`zach-a2n`**: owner `zach`, m7i.xlarge, home volume
    `vol-0ccccccccccccc001` (us-east-1a, 100 GB, the user's data). The
    instance ID changes on recreate; it was `i-0dddddddddddddd01`. The box may
    have idle-stopped.
  - State: `s3://<state-bucket>/env/zach-a2n/`. The
    platform and test-users Terraform state is **local on the devbox only**
    (`~/source/barn-platform-minimal/terraform/{platform,test-users}/terraform.tfstate`;
    test-users state holds access-key secrets). Do not plan or apply those
    roots from the laptop. Leave platform changes to the devbox, or ask the
    user how to move that state.
- **Keys on `zach-a2n`:**
  - SSH host key (ed25519): `SHA256:EXAMPLEhostKEYfingerprintAAAAAAAAAAAAAAAAAA`.
  - Owner block of `authorized_keys`: the devbox key
    `SHA256:EXAMPLEuserKEYfingerprintAAAAAAAAAAAAAAAAAA zach@devbox`.
  - Guests block: empty.

## 2. Steps on the laptop

1. **Look before changing anything** (read-only):
   - `uv tool list` and `barn --version`.
   - `~/.config/barn/config.toml` and `accounts/*.toml`.
   - `~/.aws/config` and the profile names in `~/.aws/credentials` (never
     print key values).
   - `~/.ssh/config` (`Include barn.conf` at the top), `~/.ssh/barn.conf`, and
     `ssh-keygen -F zach-a2n`.

   An old laptop setup has `aws_profile = "barn-dev"` and an account file with
   `developer_role_arn`. Both are now invalid: the role and users are gone, and
   barn rejects the old key by name.
2. **Install barn** from the up-to-date checkout:
   `uv tool install --force <checkout>` (or `git+ssh://git@git.example.com/zach/barn.git`).
   Check that `barn --help` shows `connect`.
3. **AWS profiles.**
   - The user's admin IAM user `zach` is the source profile. Its access keys
     go in `~/.aws/credentials` as `[zach]`, added by the user (for example
     `aws configure --profile zach`) if not already there; don't handle the
     secret yourself.
   - Check that `aws sts get-caller-identity --profile zach` returns
     `arn:aws:iam::123456789012:user/zach`. A `[zach]` from another account
     must not be reused.
   - Append to `~/.aws/config`, unless the profiles already exist:

     ```ini
     [profile barn-zach-manager]
     role_arn = arn:aws:iam::123456789012:role/barn-manager
     source_profile = zach
     role_session_name = zach
     region = us-east-1

     [profile barn-zach-guest]
     role_arn = arn:aws:iam::123456789012:role/barn-guest
     source_profile = zach
     role_session_name = zach
     region = us-east-1
     ```

   - Check that `aws sts get-caller-identity --profile barn-zach-manager`
     returns `assumed-role/barn-manager/zach`.
   - Remove the stale profiles `barn-dev`, `barn-dev-user` and `barn-ssm` from
     `~/.aws/config` and `~/.aws/credentials`; their IAM users are deleted.
     Back up both files first.
4. **barn home.** Back up first: `cp -a ~/.config/barn ~/.config/barn.pre-23`.
   - In `config.toml`, set `account = "sandbox"`,
     `aws_profile = "barn-zach-manager"`, `user = "zach"`, and
     `ssh_public_key` = the laptop's key. Leave the `home/` overlay as it is
     (it holds the user's tokens).
   - Write `accounts/sandbox.toml` exactly as below (it is also
     `barn-platform-minimal/handoff/accounts/sandbox.toml`):

     ```toml
     region = "us-east-1"
     state_bucket = "<state-bucket>"
     execution_instance_profile = "barn-execution"
     security_group_id = "sg-0bbbbbbbbbbbbbb03"
     subnet_ids = ["subnet-0aaaaaaaaaaaaaa06","subnet-0aaaaaaaaaaaaaa05"]
     ami_ssm_parameter = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
     manager_role_arn = "arn:aws:iam::123456789012:role/barn-manager"
     kms_key_id = ""

     [extra_tags]
     ```

   - Delete the old `state/<user>-<env>/` data dirs under `~/.config/barn/`
     (e.g. `state/zach-a2n`, `state/zach-e2e`). Their S3 objects are gone, and
     barn recreates `state/zach-a2n/` with the new key. Keep
     `state/plugin-cache/`.
   - Run `barn init`; it must report nothing required.
5. **SSH key:** the laptop uses the same key pair as the devbox (user,
   2026-11-02), so it is already `zach-a2n`'s owner key. Confirm that
   `ssh-keygen -lf <ssh_public_key>` prints
   `SHA256:EXAMPLEuserKEYfingerprintAAAAAAAAAAAAAAAAAA`. If it does not, stop
   and ask: running `barn create zach-a2n` with a different key would replace
   the instance (user data holds the owner key).
6. **Verify:**
   - `barn list` shows `zach-a2n`, owner `zach (you)`. If it is stopped,
     `barn start zach-a2n` (ask first; it costs money).
   - `barn connect zach-a2n`, then `ssh zach-a2n hostname`.
   - If `known_hosts` already has `zach-a2n`, there must be no host-key
     warning: the key must be `SHA256:Sk4G...` above. On "REMOTE HOST
     IDENTIFICATION HAS CHANGED", stop and report; do not run `ssh-keygen -R`.
   - `barn doctor zach-a2n`: all checks pass as `barn-manager` (51 on the
     devbox).
7. **Tidy `~/.ssh/barn.conf`:** remove leftover blocks for pre-23 aliases (any
   `# >>> barn zach-<env> >>>` block other than `zach-a2n`). barn only rewrites
   blocks for names it is asked about.
8. **Tokens:** the box already has working Claude and Kiro tokens. Run
   `barn sync zach-a2n` from the laptop only if the user wants the laptop's
   `home/` overlay pushed; local files overwrite the box's copies.

## 3. Not needed on the laptop

- The test people (`barn-platform-minimal/e2e/people/`), the `alice`/`foo`
  profiles and keys, and the platform and test-users roots. These are devbox
  test fixtures.
