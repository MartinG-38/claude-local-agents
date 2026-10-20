# Security policy

`/local` runs code written by local models on your machine, so its guard, sandboxes and scope checks
matter. Thank you for helping keep them sound.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/MartinG-38/claude-local-agents/security/advisories/new).

Useful to include: what an agent or a checked command could do that it should not (read a secret,
write outside `--write`, reach the network, use Ollama's cloud, escape the sandbox…), your OS and
versions (`/local doctor` output), and the smallest instruction or file that reproduces it.

You can expect a first answer within a week. Fixes are released as soon as possible and credited in
the [changelog](CHANGELOG.md) unless you prefer otherwise.

## Scope

In scope: `guard.py` (the PreToolUse hook), the check sandboxes (`sandbox-exec`, `bubblewrap`), the
environment scrubbing, the cloud refusal, the scope checks (`--write`, held-out tests), the installer.

Known and documented limits (see "Known limitations" in the README) are not vulnerabilities by
themselves: for example, no sandbox on Windows unless `LOCAL_ALLOW_UNSANDBOXED=1` is set, or the
macOS profile being less strict than bubblewrap. A way around them that the documentation does not
describe is welcome.

## Supported versions

Only the latest version on `main` receives fixes.
