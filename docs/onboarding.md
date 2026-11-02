# Onboarding an AWS account and its people

How an AWS account is set up for barn, and how people are added to and
removed from it. What the account must contain, and why, is in
[account-prerequisites.md](account-prerequisites.md); this page is the order
of work.

## The account's platform

barn does not set up accounts. Each account needs a **platform**: the
infrastructure code, owned by the account's administrators, that creates the
prerequisites (network, security group, execution instance profile, state
bucket, the manager and guest roles and their policies) and says who may
assume which role. It is maintained outside barn, typically one repo per
account so each account's state is separate. Before anyone can use barn in an
account, its platform must already be applied.

> **For agents:** do not assume where an account's platform lives. Ask the
> user for its location (repo, path, and the admin profile to apply it with)
> before reading or changing it, and confirm every apply with them.

## A new account

1. **Set up the platform.** Create it for the account (or extend the
   account's existing infrastructure code) so that it provides everything in
   the [checklist](account-prerequisites.md#checklist), with the
   [manager](account-prerequisites.md#manager-permissions) and
   [guest](account-prerequisites.md#guest-permissions) policies attached to
   the roles people assume. If the roles are owned by central automation (for
   example SSO permission sets), attach the policies there: a policy attached
   from outside may be silently removed.
2. **Apply it** as an admin of the account, reviewing the plan first.
3. **Keep its state safe.** If the platform uses local state, it exists only
   on the machine that applied it. Back it up, or keep it in a remote backend.
   If it lives in the account's state bucket, use a key **outside `env/`**:
   managers can read and write `env/*`.
4. **Write the account file** (`accounts/<account>.toml`, see
   [The account file](account-prerequisites.md#the-account-file)) from the
   platform's values. It holds no secrets; one copy is shared with everyone
   using the account, and the platform repo is a good place to keep it.
5. **Verify** as the first manager (set up as below):

   ```bash
   barn create <you>-test
   barn doctor <you>-test        # all green, including the manager policy simulation
   barn destroy <you>-test --volume
   ```

   If `create` times out waiting for SSM, check the subnets' egress, the
   security group's egress and the instance profile
   ([Verify](account-prerequisites.md#verify)).

## Adding a person

### In the platform (administrator)

- **IAM users assuming the roles:** the person needs an IAM user in the
  account, with access keys they create themselves. Add the user to the trust
  policy of `barn-manager`, `barn-guest`, or both, and apply; the change should
  touch only the trust policies. Within one account, a trust policy naming the
  user is enough for them to assume the role, unless a permission boundary or
  SCP says otherwise.
- **SSO or existing roles:** grant the person the role or permission set the
  policies are attached to. Their normal SSO profile is their barn profile.

Send them the account file and, for IAM users, the role ARNs (or ready-made
`~/.aws/config` snippets, if the platform produces them).

### On the person's machine

1. **AWS profiles.** For IAM users: access keys in `~/.aws/credentials` as
   `[<user>]` (`aws configure --profile <user>`), and one profile per role in
   `~/.aws/config`:

   ```ini
   [profile barn-<user>-manager]
   role_arn = arn:aws:iam::<account-id>:role/barn-manager
   source_profile = <user>
   role_session_name = <user>      # CloudTrail shows who used the role
   region = <region>
   ```

   `aws sts get-caller-identity --profile barn-<user>-manager` must return
   `assumed-role/barn-manager/<user>`.
2. **barn.** [Install](../README.md#install) it, then:

   ```bash
   barn init --account-file <account>.toml --aws-profile barn-<user>-manager   # or the guest profile
   ```

   Set `user` and `ssh_public_key` in `~/.config/barn/config.toml`, run
   `barn init` again until nothing is required, then `barn list`.

   Already using barn with another account? `barn init --account-file
   <account>.toml` adds the account file and keeps `config.toml`; add
   `<account> = "barn-<user>-manager"` under its `[aws_profiles]`. Commands
   find existing boxes in it on their own; `barn create` needs
   `--account <account>` (or change `default_account`).
3. **Guests** also need a box owner to add their public key to the owner's
   `[guest_keys]` list for this account and run `barn sync <name>`. Then `barn connect <name>` and
   plain `ssh <name>` work ([Guests](../README.md#guests)).

## Removing a person

1. Box owners remove the person's key from the account's `[guest_keys]` list and run
   `barn sync <name>` on each box. This revokes only the key barn wrote; a key
   added by hand outside barn's blocks in `~/.ssh/authorized_keys` stays (see
   [What the policies do not protect](account-prerequisites.md#what-the-policies-do-not-protect)).
2. In the platform, remove the person from the roles' trust policies and
   apply **before** deleting their IAM user. If the user is deleted first, IAM
   rewrites the trust policy's ARN into the user's unique ID, and the
   platform's next plan shows a diff.
3. Delete the IAM user and its access keys (or, for SSO, the assignment).

Boxes the person owned stay, with `barn:owner` naming them. Any manager can
still stop or destroy them (`destroy --force`); take anything you need off the
home volume first.
