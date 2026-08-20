# git-remote-s3

## About this fork

This is a fork of [awslabs/git-remote-s3](https://github.com/awslabs/git-remote-s3), licensed under the
Apache License 2.0 (unchanged). Notable additions in this fork:

- Fix for LFS temp-file paths when the repo is used as a submodule
- Per-remote LFS scoping, so a repo can mix an S3 LFS remote with non-S3 remotes
- Auto-install of the LFS transfer agent on first remote-helper run, scoped per remote (never the repo-wide `lfs.standalonetransferagent`)
- Pushes no longer stall ~10s per push on git-lfs's pure-SSH endpoint probe of the `s3://` URL — the auto-install writes `remote.<name>.lfsurl`, which suppresses it
- DNS TXT bucket-alias resolution for `s3://` remote URIs
- S3 Access Grants support, region-aware S3 clients, and a `git-s3 doctor` diagnostic command
- Doctor repairs are safe for nested remote prefixes (e.g. `s3://bucket/team/repo`): keys are parsed relative to the repo prefix, and the LFS object store is never mistaken for a branch
- Pushing from a shallow clone is rejected with a clear error telling you to run `git fetch --unshallow` first, instead of silently uploading a truncated pack
- Partial clones (`git clone --filter=blob:none` / `--filter=tree:0`) are fully supported for push and fetch
- Push and fetch render live transfer progress on the terminal, honoring `git push --quiet` / `--progress`
- `--force-with-lease` is supported with real compare-and-swap semantics against the remote ref, not just a `+` force push

Not affiliated with or endorsed by Amazon Web Services.

This fork is published on PyPI as `fduplex-git-remote-s3`, but it installs the same `git-remote-s3`
command as upstream and therefore replaces it, so a given environment should install
`fduplex-git-remote-s3` or upstream `git-remote-s3`, not both.

This library enables to use Amazon S3 as a git remote and LFS server.

It provides an implementation of a [git remote helper](https://git-scm.com/docs/gitremote-helpers) to use S3 as a serverless Git server.

It also provide an implementation of the [git-lfs custom transfer](https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md) to enable pushing LFS managed files to the same S3 bucket used as remote.

## Table of Contents

- [Installation](#installation)
- [Prerequisites](#prerequisites)
- [Security](#security)
  - [Data encryption](#data-encryption)
  - [Access control](#access-control)
- [Use S3 remotes](#use-s3-remotes)
  - [Create a new repo](#create-a-new-repo)
  - [Clone a repo](#clone-a-repo)
  - [DNS bucket aliases](#dns-bucket-aliases)
  - [Bucket region cache](#bucket-region-cache)
  - [S3 Access Grants](#s3-access-grants)
  - [Branches, etc.](#branches-etc)
  - [Using S3 remotes for submodules](#using-s3-remotes-for-submodules)
- [LFS](#lfs)
  - [Fetching through a facade URL (uv and friends)](#fetching-through-a-facade-url-uv-and-friends)
  - [Creating the repo and pushing](#creating-the-repo-and-pushing)
  - [Clone the repo](#clone-the-repo)
- [Notes about specific behaviors of Amazon S3 remotes](#notes-about-specific-behaviors-of-amazon-s3-remotes)
  - [Arbitrary Amazon S3 URIs](#arbitrary-amazon-s3-uris)
  - [Concurrency and locking](#concurrency-and-locking)
- [Manage the Amazon S3 remote](#manage-the-amazon-s3-remote)
  - [Delete branches](#delete-branches)
  - [Protected branches](#protected-branches)
- [Under the hood](#under-the-hood)
  - [How S3 remote work](#how-s3-remote-work)
  - [How LFS work](#how-lfs-work)
  - [Debugging](#debugging)
- [Credits](#credits)

## Installation

`git-remote-s3` is a Python script and works with any Python version >= 3.9.

Run:

```
pip install fduplex-git-remote-s3
```

## Prerequisites

Before you can use `git-remote-s3`, you must:

- Complete initial configuration:

  - Creating an AWS account
  - Configuring an IAM user or role

- Create an AWS S3 bucket (or have one already) in your AWS account.
- Attach a minimal policy to that user/role that allows the to the S3 bucket:

  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "S3ObjectAccess",
        "Effect": "Allow",
        "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
        "Resource": ["arn:aws:s3:::<BUCKET>/*"]
      },
      {
        "Sid": "S3ListAccess",
        "Effect": "Allow",
        "Action": ["s3:ListBucket"],
        "Resource": ["arn:aws:s3:::<BUCKET>"]
      }
    ]
  }
  ```

- Optional (but recommended) - use [SSE-KMS Bucket keys to encrypt the content of the bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-key.html), ensure the user/role create previously has the permission to access and use the key.

```json
{
  "Sid": "KMSAccess",
  "Effect": "Allow",
  "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
  "Resource": ["arn:aws:kms:<REGION>:<ACCOUNT>:key/<KEY_ID>"]
}
```

- Install Python and its package manager, pip, if they are not already installed. To download and install the latest version of Python, [visit the Python website](https://www.python.org/).
- Install Git on your Linux, macOS, Windows, or Unix computer.
- Install the latest version of the AWS CLI on your Linux, macOS, Windows, or Unix computer. You can find instructions [here](https://docs.aws.amazon.com/cli/latest/userguide/installing.html).

## Security

### Data encryption

All data is encrypted at rest and in transit by default. To add an additional layer of security you can use customer managed KMS keys to encrypt the data at rest on the S3 bucket. We recommend to use [Bucket keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucket-key.html) to minimize the KMS costs.

### Access control

Access control to the remote is ensured via IAM permissions, and can be controlled at:

- bucket level
- prefix level (you can use prefixes to store multiple repos in the same S3 bucket thus minimizing the setup effort)
- KMS key level

If you store multiple repos in a single bucket but would like to separate permissions to access each repo, you can do so by modifying the resource definitions for the object related action to specify the repo prefix and by adding a condition to the ListBucket action to restrict the operation to matching prefixes (and by consequence the corresponding repo) :

```json
      {
        "Sid": "S3ObjectAccess",
        "Effect": "Allow",
        "Action": [
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:AbortMultipartUpload"
        ],
        "Resource": ["arn:aws:s3:::<BUCKET>/<REPO>/*"]
      },
      {
        "Sid": "S3ListObjects",
        "Effect": "Allow",
        "Action": [
          "s3:ListBucket",
        ],
        "Condition": {
          "StringEquals": {
            "s3:prefix": "<REPO>"
          }
        },
        "Resource": ["arn:aws:s3:::<BUCKET>"]
      },
```

Using the condition key restricts the access operation to the content of the specific repo in the bucket.

## Use S3 remotes

### Create a new repo

S3 remotes are identified by the prefix `s3://` and at the bare minimum specify the name of the bucket. You can also provide a key prefix as in `s3://my-git-bucket/my-repo` and a profile `s3://my-profile@my-git-bucket/myrepo`.

```bash
mkdir my-repo
cd my-repo
git init
git remote add origin s3://my-git-bucket/my-repo
```

You can then add a file, commit and push the changes to the remote:

```bash
echo "Hello" > hello.txt
git add -A
git commit -a -m "hello"
git push --set-upstream origin main
```

The remote HEAD is set to track the branch that has been pushed first to the remote repo. To change the remote HEAD branch, run `git-s3 head s3://<bucket>/<prefix> <branch>`.

`s3+zip://` is accepted for back-compat but is deprecated and now behaves exactly like `s3://`: no `repo.zip` archive is written. Use `s3://` for new remotes.

### Clone a repo

To clone the repo to another folder just use the normal git syntax using the s3 URI as remote:

```bash
git clone s3://my-git-bucket/my-repo my-repo-clone
```

### DNS bucket aliases

> **Fork addition** (not in upstream awslabs/git-remote-s3): the bucket component of the remote URI can be a DNS hostname aliasing the real bucket.

When the bucket component of the remote URI contains at least one dot, it is treated as a DNS hostname instead of a literal bucket name (bucket names used with this feature must not contain dots). The hostname is resolved to the real bucket name via a DNS TXT lookup using the system resolver, so split-horizon/VPN DNS setups work as usual:

- A TXT record must exist at the alias hostname itself.
- Among its TXT values, exactly one must have the form `git-bucket=<real-bucket-name>`; other TXT values at the same name are ignored.

For example, with the record

```
repos.git.example.com. 300 IN TXT "git-bucket=my-git-bucket-123456789012-us-east-2"
```

the following commands are equivalent:

```bash
git clone s3://repos.git.example.com/my-repo
git clone s3://my-git-bucket-123456789012-us-east-2/my-repo
```

Aliases work in every place a remote URI is accepted: the git remote helper, the `git-lfs-s3` transfer agent and `git-lfs-s3 install --remote`, and the `git-s3` management CLI. Resolution results are cached for the lifetime of the process. If the alias has no TXT record or no `git-bucket=` value, the command fails with an error describing the expected record instead of falling back to using the hostname as a bucket name.

Alias resolution is enabled by default and can be disabled via git config, so a dotted bucket component is treated as a literal bucket name again:

```bash
# per remote (takes precedence when set):
git config remote.origin.s3-dns-alias false
# for all remotes (used when the per-remote key is unset or no remote name is known):
git config s3.dns-alias false
```

Both keys are booleans; setting the per-remote key to `true` re-enables aliasing for that remote even when `s3.dns-alias` is `false`. The per-remote key applies where a remote name is available (the git remote helper, the LFS transfer agent, `git-lfs-s3 install --remote`); the `git-s3` CLI takes a URI rather than a remote name and honors only `s3.dns-alias`.

### Bucket region cache

> **Fork addition** (not in upstream awslabs/git-remote-s3): the bucket's region is detected once and remembered in the repo's local git config.

Every S3 client the remote helper builds is pinned to the bucket's own region, which otherwise costs a `HeadBucket` round trip on every single git command. The first successful detection is written to the repo-local git config as `remote.<name>.s3region`, and every later invocation reads it from there instead:

```bash
git config --get remote.origin.s3region
# eu-west-1
```

The value is written on the first clone, fetch or push against a remote, and only for a real remote name — a push straight to a URI (`git push s3://bucket/repo ...`) detects the region and does not cache it. For submodules the key lands in the submodule's own config under `.git/modules/<name>/config`.

If a bucket ever moves to another region, the cached value goes stale. The helper notices the redirect S3 returns, drops the key and retries once, so the operation still succeeds; you can also clear it by hand:

```bash
git config --unset remote.origin.s3region
```

### Imported-entry high-water mark

> **Fork addition** (not in upstream awslabs/git-remote-s3): a fetch remembers how far down the manifest's entry log it has already imported.

`remote.<name>.gitwal-seq` records the highest `gitwal.json` entry seq imported into this clone, so a routine fetch downloads only the packs added since. It is a hint, never state: after importing, the client verifies the fetched tips with `git rev-list --objects` and, when they do not resolve, keeps pulling older entries until they do. A stale or hand-edited value costs a round trip, never correctness, and the key is written only after verification passes.

```bash
git config --get remote.origin.gitwal-seq
# 43
```

As with the region cache, a fetch straight to a URI has no remote section to write to and re-imports from the start of the log.

### S3 Access Grants

> **Fork addition** (not in upstream awslabs/git-remote-s3): the [AWS S3 Access Grants boto3 plugin](https://github.com/awslabs/aws-s3-access-grants-plugin-boto3) is bundled and auto-registered on every S3 client this tool builds — the git remote helper, the `git-lfs-s3` transfer agent, and the `git-s3` management CLI.

Registration is transparent and always runs with fallback enabled, so a single code path serves both credential models:

- A caller whose identity holds an S3 Access Grant gets short-lived, prefix-scoped credentials vended by Access Grants for each S3 operation.
- A caller using plain IAM credentials (an access-key user or a role with direct S3 policy access and no grant) transparently falls back to a direct S3 call — no configuration needed.

On the first fallback in a process a one-time notice is printed to stderr; it points you at `git-s3 doctor` (below) if you *expected* Access Grants to be used.

#### IAM permissions for the Access Grants path

To use Access Grants, the caller role/identity needs **both** of these actions on the Access Grants instance resource:

- `s3:GetDataAccess` — vends the scoped credentials.
- `s3:GetAccessGrantsInstanceForPrefix` — resolves which account owns the Access Grants instance for the requested `s3://bucket/prefix`.

The plugin calls `GetAccessGrantsInstanceForPrefix` **before** it can call `GetDataAccess`, because it must first learn the owner account id to target. This is a separate IAM action that is easy to overlook: if the caller has `s3:GetDataAccess` but not `s3:GetAccessGrantsInstanceForPrefix`, the plugin fails during that preflight and — because fallback is enabled — **silently** drops to direct S3 credentials. The user then sees only a misleading downstream `AccessDenied` from the direct call (or a successful direct call that never used Access Grants at all), with nothing pointing at the real cause. Grant both actions together.

#### Diagnosing with `git-s3 doctor`

`git-s3 doctor <remote>` runs an Access Grants entitlement check as its own section. Unlike the normal path, this check runs the plugin with fallback **disabled** and drives the full vend path (including the `GetAccessGrantsInstanceForPrefix` preflight) against the repo's prefix, so it surfaces the real error the fallback would otherwise hide. It reports:

- `Access Grants: OK` when credentials were vended for the repo prefix.
- `Access Grants: not available (using direct S3 credentials)` on an `AccessDenied`, naming the exact failing operation and the missing permission — e.g. `caller role is missing s3:GetAccessGrantsInstanceForPrefix` or `caller role is missing s3:GetDataAccess or has no matching grant`.

This is informational: an IAM-key user with no grant legitimately reports "not available" and keeps working via direct credentials — that is expected, not an error.

#### Bucket region auto-detection

The S3 client is automatically pinned to the bucket's real region, detected via a `HeadBucket` probe (which returns the region even for an unauthorized caller, so it needs no extra permission and is cached per process). You do **not** need your default region to match the bucket's region; if the region cannot be determined the tool proceeds with your default region and S3's cross-region redirects, exactly as before.

### Branches, etc.

Creating branches and pushing them works as normal:

```bash
cd my-repo
git checkout -b new_branch
touch new_file.txt
git add -A
git commit -a -m "new file"
git push origin new_branch
```

All git operations that do not rely on communication with the server should work as usual (eg `git merge`)

### Using S3 remotes for submodules

If you have a repo that uses submodules also hosted on S3, you need to run the following command:

```
git config protocol.s3.allow always
```

Or, to enable globally:

```
git config --global protocol.s3.allow always
```

## LFS

To use LFS you need to first install git-lfs. You can refer to the [official documentation](https://git-lfs.com/) on how to do this on your system.

Next, enable the S3 integration in the repo. There are two install modes:

```bash
# Per-remote (recommended; required when other LFS remotes coexist)
git-lfs-s3 install --remote <remote-name>

# Unscoped (back-compat; applies the agent to ALL remotes in the repo)
git-lfs-s3 install
```

`--remote` writes a per-remote scoped configuration so `git-lfs-s3` only fires for that one remote — letting an S3 remote coexist with non-S3 LFS remotes (e.g. GitHub, GitLab) without breaking their LFS push/pull. Use it whenever the repo has more than one remote.

The bare `git-lfs-s3 install` form sets `lfs.standalonetransferagent` globally and is short for:

```bash
git config --add lfs.customtransfer.git-lfs-s3.path git-lfs-s3
git config --add lfs.standalonetransferagent git-lfs-s3
```

`git-lfs-s3 install --remote <name>` instead writes:

```bash
git config remote.<name>.lfsurl https://lfs-alias.git-remote-s3.test/<bucket>/<prefix>
git config lfs.<that-url>.standalonetransferagent git-lfs-s3
git config lfs.customtransfer.git-lfs-s3.path git-lfs-s3
```

The `lfs-alias.git-remote-s3.test` host is a synthetic, never-contacted match key (the `.test` TLD is reserved by RFC 6761 for non-resolvable use). It exists only because git-lfs's URL parser does not natively understand `s3://` URLs and would otherwise fall back to SSH-style endpoint discovery; setting `remote.<name>.lfsurl` short-circuits that path and gives the scoped agent lookup a stable URL to match against.

`<bucket>` in that URL is the bucket component of the remote URL verbatim: when the remote uses a DNS bucket alias (e.g. `s3://demos.git.example.com/my-repo`), the alias — not the resolved bucket name — is written into the config, so re-pointing the alias at a different bucket never invalidates existing checkouts. Re-running `git-lfs-s3 install --remote <name>` migrates configs written by older versions that rendered the resolved bucket name.

`lfs.customtransfer.git-lfs-s3.path` is necessarily repo-wide (git-lfs registers transfer adapters globally, not per URL), so `git-lfs-s3` is still listed in the `transfers` array of batch requests sent to other LFS servers. If a server rejects requests naming unknown adapters, set:

```bash
git config lfs.basictransfersonly true
```

which makes git-lfs omit the `transfers` array entirely. This setting is repo-wide and limits other remotes to basic HTTPS transfers; GitHub-style hosts already use these (even over SSH remotes), so only the rare server that speaks exclusively the pure-SSH LFS protocol is affected.

### Fetching through a facade URL (uv and friends)

Some tools invoke git-lfs with a URL instead of a remote name, in a directory that has no remotes configured at all: `uv` does exactly this for a git dependency with `lfs = true`, running `git lfs fetch <url> <sha>` inside its own cache directory. git-lfs passes that URL to the transfer agent verbatim, before any rewriting.

For those callers, map the facade URL to the S3 remote with git's standard URL rewriting:

```bash
git config --global url."s3://<bucket>/<prefix>".insteadOf https://git.example.com/<prefix>
git config --global lfs.customtransfer.git-lfs-s3.path git-lfs-s3
git config --global lfs."https://s3".standalonetransferagent git-lfs-s3
```

The agent re-applies the `insteadOf` mapping itself (longest matching prefix wins, same as git) to recover the `s3://` URI, so the fetch works with no remote configured. Without a matching `insteadOf` entry it fails the transfer with an error naming the URL it could not map.

### Creating the repo and pushing

Let's assume we want to store TIFF file in LFS.

```bash
mkdir lfs-repo
cd lfs-repo
git init
git lfs install
git remote add origin s3://my-git-bucket/lfs-repo
git-lfs-s3 install --remote origin
git lfs track "*.tiff"
git add .gitattributes
<put file.tiff in the repo>
git add file.tiff
git commit -a -m "my first tiff file"
git push --set-upstream origin main
```

### Clone the repo

```bash
git clone s3://my-git-bucket/lfs-repo lfs-repo-clone
```

`git-remote-s3` installs the LFS transfer agent in the new repo's local config on first invocation, so `git clone` and `git submodule add` work without extra setup. It writes exactly the same per-remote keys as `git-lfs-s3 install --remote <name>` — `lfs.customtransfer.git-lfs-s3.path`, `remote.<name>.lfsurl` and the URL-scoped `lfs.<url>.standalonetransferagent` — and never the repo-wide `lfs.standalonetransferagent`. Set `GIT_REMOTE_S3_AUTO_INSTALL_LFS=0` to opt out; an existing `lfs.standalonetransferagent` naming another agent, or an existing `remote.<name>.lfsurl`, suppresses the install entirely, and nothing is written for a remote that is not an `s3://` URL.

## Notes about specific behaviors of Amazon S3 remotes

### Arbitrary Amazon S3 URIs

An Amazon S3 URI for a valid bucket and an arbitrary prefix which does not contain the right structure under it, is considered valid.

`git ls-remote` returns an empty list and `git clone` clones an empty repository for which the S3 URI is set as remote origin.

```
% git clone s3://my-git-bucket/this-is-a-new-repo
Cloning into 'this-is-a-new-repo'...
warning: You appear to have cloned an empty repository.
% cd this-is-a-new-repo
% git remote -v
origin  s3://my-git-bucket/this-is-a-new-repo (fetch)
origin  s3://my-git-bucket/this-is-a-new-repo (push)
```

**Tip**: This behavior can be used to quickly create a new git repo.

### Concurrency and locking

`git-remote-s3` has no locks. Every ref in the repo — branch, tag, and HEAD — lives in a single object, `<prefix>/gitwal.json`, and every write to that repo is one conditional PUT against it: `If-None-Match: *` to create it, `If-Match: <etag>` to update it. S3 only accepts the PUT if the etag still matches what the client read, so two pushers racing each other cannot both win. The loser's PUT is rejected with a precondition failure, and `git-remote-s3` reloads the manifest, re-checks the push against the refs it now names (fast-forward, `--force`, `--force-with-lease`, protected-branch), and retries the whole decision from scratch. There is nothing to time out, nothing to expire, and nothing to clean up by hand.

The objects a push uploads — packs under `<prefix>/packs/<sha>.pack`, LFS blobs under `<prefix>/lfs/<oid>` — are content-addressed and written before the manifest CAS, so they are immutable and safe to upload from multiple clients at once; the manifest PUT is the single serialization point that decides which pack(s) actually become part of a ref's history. A pack uploaded by a push that loses the race is simply never referenced by any entry and becomes an orphan, reclaimed the next time someone runs `git-s3 compact`. No data is lost and no ref is ever left pointing at more than one place.

`git-s3 doctor <s3-uri>` audits a repo's manifest and packs (schema validation, missing packs, orphan packs, whether compaction is due) without writing anything.

## Manage the Amazon S3 remote

### Delete branches

To remove remote branches that are not used anymore you can use the `git-s3 delete-branch <s3uri> <branch_name>` command. This is a refs-only change: a conditional PUT drops the branch from the manifest. The packs it uniquely referenced stay in the bucket until `git-s3 compact` reclaims them.

### Protected branches

To protect/unprotect a branch run `git s3 protect <remote> <branch-name>` respectively `git s3 unprotect <remote> <branch-name>`.

## Under the hood

### How S3 remote work

A repo is one manifest object, `<prefix>/gitwal.json`, plus the packs it names under `<prefix>/packs/<sha>.pack`. The manifest is the sole authority for what a ref points to: it lists every branch and tag, the HEAD, the protected refs, and a log of entries, each naming a pack and the tips that pack makes reachable.

Listing refs (`git ls-remote`, the start of a clone or fetch) reads the manifest, no bucket listing required.

Pushing packs the new objects with `git pack-objects`, excluding whatever the manifest already has, uploads the pack to its content-addressed key, then commits with a single conditional PUT to `gitwal.json` (see [Concurrency and locking](#concurrency-and-locking)). The PUT is the only step that can fail on a race; the pack upload before it is inert until an entry in the manifest names it.

Because entries accumulate with every push, the log slowly grows one pack per push and the same objects can end up duplicated across several packs. `git-s3 compact <remote>` collapses the whole log into a single base pack covering every current ref, then deletes the packs it superseded.

### How LFS work

The LFS integration stores the file in the bucket defined by the remote URI, under a key `<prefix>/lfs/<oid>`, where oid is the unique identifier assigned by git-lfs to the file.

If an object with the same key already exists, git-lfs-s3 does not upload it again.

### Debugging

Use `--verbose` flag or set `transfer.verbosity=2` to print debug information when performing git operations:

```bash
git -c transfer.verbosity=2 push origin main
```

For early errors (like credential issues), use the environment variable:

```bash
GIT_REMOTE_S3_VERBOSE=1 git push origin main
```

Logs will be put to stderr.

For LFS operations you can enable and disable debug logging via `git-lfs-s3 enable-debug` and `git-lfs-s3 disable-debug` respectively. Logs are put in `.git/lfs/tmp/git-lfs-s3.log` in the repo.

## Credits

The git S3 integration was inspired by the work of Bryan Gahagan on [git-remote-s3](https://github.com/bgahagan/git-remote-s3).

The LFS implementation benefitted from [lfs-s3](https://github.com/nicolas-graves/lfs-s3) by [@nicolas-graves](https://github.com/nicolas-graves). If you do not need to use the git-remote-s3 transport you should use that project.
