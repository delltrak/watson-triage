<p align="center">
  <img src="docs/assets/watson-agent-index-cover.png" alt="Watson — GitHub issues, investigated. Right in iMessage. Built with Plow." width="100%">
</p>

# Watson

Built by [Deltrak](https://github.com/delltrak).

**Your assigned GitHub issues arrive investigated.**

Watson reads an issue, its conversation, relevant source files and GitHub Actions results. It remembers earlier investigations, asks the author for missing information in the issue's language, and resumes when they reply. Owner updates are in Brazilian Portuguese over Plow/iMessage.

This is a local-first prototype using the user's existing Codex/ChatGPT session. It never merges PRs.

## What is implemented

- Persistent SQLite issue history, pending questions and workflow state.
- Polling cycles that discover new assignments and resume after new comments or code revisions.
- Evidence-based source analysis and CI job/failed-step/artifact metadata. CI logs remain linked; raw logs and arbitrary artifacts are not automatically ingested.
- Author mentions in the issue language, with deduplication of Watson's own comments.
- Test-account requests through an agreed private channel. Credentials are entered locally, not posted to GitHub or sent to the model.
- Playwright execution of owner-approved scenarios in a test environment; screenshots, local trace, and MP4 recordings.
- Yellow narrative subtitles burned into the video, with a dark outline for readability. Captions use the issue's language and recorded actions/results; timestamps come from the browser runner. An SRT sidecar is saved locally. This is text narration, without a spoken soundtrack.
- Login runs in a separate, unrecorded browser context. Browser requests are restricted to the configured origin. Traces stay private because they may contain authenticated request metadata.
- iMessage text/video delivery to the verified owner of the configured Plow line.
- Sol requested through ChatGPT Read Aloud, adapted from Deca. Native voice memo delivery is pending Plow support: [upstream issue #199](https://github.com/plow-pbc/hermes-plugin-plow/issues/199).
- Explicit repair command: scoped changes, regression tests in a container without network or host credentials, and a draft PR. A new regression test must fail against the original source and pass with the fix. No merge operation exists.
- Official Agent Index client pinned and bundled, with an adapter for measured Watson-only Codex usage.

Browser validation is configured per issue. A human supplies the trusted test environment and login selectors. Scenarios may be supplied explicitly, or `auto_plan: true` lets Codex propose steps from the issue and controls actually observed after login. Generated selectors must match observed controls and the plan must contain an expected-result assertion. This bounded planner supports simple forms, not arbitrary website exploration. Without a browser profile, Watson performs static triage and requests missing information.

## Install

Requires Python 3.11+, GitHub CLI, and a Plow credential for inference — mint one with the [plow-agents CLI](https://github.com/plow-pbc/plow-agents). Browser recording requires the optional browser dependencies. Docker is needed only for isolated repair tests.

```sh
git clone https://github.com/delltrak/watson-triage.git
cd watson-triage
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[browser]'
.venv/bin/python -m playwright install chromium
gh auth login
plow-agents mint ln_xxx --credential-file ./plow-credentials
set -a; . ./plow-credentials; set +a
.venv/bin/watson --home .watson init --repo owner/repo --assignee your-login --delivery text
.venv/bin/watson --home .watson sync
.venv/bin/watson --home .watson track 123
.venv/bin/watson --home .watson cycle
```

Inference reads `PLOW_API_BASE` and `HERMES_CUSTOM_PLOW_API_KEY` (falling back
to `PLOW_AGENT_TOKEN`) from the environment, which is what sourcing the minted
credential above supplies — or `plow_credential_file` from the config below,
which delivery already uses. Without one of those, the first investigation
fails with `Sem credencial de inferência`. There is no `codex login` step:
Codex was the inference backend until this change.

The initial sync records a baseline without processing the entire backlog. Explicitly track existing issues. Later assignments are tracked automatically. `cycle` performs one bounded round; use a local supervisor/scheduler to run it periodically. The owner's pilot uses a Codex heartbeat; installation does not silently install a daemon.

By default, GitHub comments, owner delivery and repairs are disabled. Enable only the capabilities you want in the private `.watson/config.json`:

```json
{
  "github_comments": true,
  "notify_owner": true,
  "send_video": true,
  "audio_mode": "native",
  "plow_credential_file": "/absolute/private/path/plow-credentials",
  "private_access_channel": "a private message to the repository owner",
  "cycle_limit": 3
}
```

These are additional keys; preserve the repository, assignee and other fields created by `init`. Mint the line-scoped credential with the official [plow-agents CLI](https://github.com/plow-pbc/plow-agents). Never commit it.

GitHub writes use the logged-in user's identity. Use a dedicated GitHub account/app if Watson needs a separate identity. Comment publishing is only enabled for the configured primary repository. Related repositories are read-only evidence sources.

## Browser validation and test access

Add a `browser_profiles` entry keyed by issue number, such as the example in [docs/browser-profile.json](docs/browser-profile.json). The profile is trusted owner configuration, never taken directly from issue comments. Set `source_sha` to the commit deployed to that test instance. Watson refuses to validate it as the current default branch if that revision differs.

Supported steps are `fill`, `click`, and `expect_text`. At least one explicit expected-result assertion is required. Only same-origin requests are permitted. Use synthetic data and a dedicated test account.

When credentials are missing, Watson asks the author to arrange access privately. The repository owner enters them using:

```sh
.venv/bin/watson --home .watson access 123
.venv/bin/watson --home .watson cycle
```

The password prompt is hidden. Values are stored in a local mode-600 file. They are not automatically collected from iMessage or GitHub. The next cycle detects the new access and the author's reply and resumes the case. Plain local files are not a full secret manager; use an isolated runtime and filesystem encryption for sensitive environments.

Artifacts live under `.watson/evidence`. Only the short video is optionally sent to the owner; traces are not uploaded. A failed assertion means the configured scenario failed. A passing test does not prove deployment or universal correctness.

## Corrections and draft PRs

Opt in with a `repair` configuration containing `enabled: true` and an explicit `allowed_files` list. The current repair runner supports Python projects tested with `python -m unittest discover -s tests -v` in a pinned Docker image. Other stacks, including the React pilot, need their own reviewed test runner before enabling repair.

```sh
.venv/bin/watson --home .watson repair 123
```

A reproduced failure is required. Source changes are proposed by Codex without tool access, then checked against the allowlist. Tests run with no network, a read-only source mount, no capabilities and no host credentials. A successful proposal creates a new branch and draft PR against the observed base revision. PR creation is explicit; `cycle` does not automatically fix every issue. No automatic merge or deployment.

## Audio

```sh
.venv/bin/watson --home .watson voice 1 --output summary.mp3
```

The ChatGPT internal Read Aloud endpoint receives `voice=sol` and Portuguese text. It uses the existing file-backed Codex session, without a public API key. This is an internal endpoint and may change or ignore voice selection. It does not refresh tokens and does not support keychain-only authentication. The speech text is sent to ChatGPT; credentials are not copied into memory. An error never silently switches to macOS speech.

Native iMessage voice bubbles need a Plow backend operation that is not in its published API as inspected on 2026-09-15. `audio_mode=native` blocks file substitution. If the owner explicitly accepts regular files, `audio_mode=attachment` enables the legacy attachment delivery. Text/video workflow updates remain usable while native voice is pending.

## Agent Index

The bundled official client is unmodified, pinned at `87901f8b182a8a7c65ee3dd7267f8f835ee2a545` (Apache-2.0; included NOTICE/license). Watson records every measured Codex invocation separately, including unsuccessful attempts that returned usage. Cached input is separated from total input to avoid double-counting.

Use an explicit model in `init --model YOUR_CODEX_MODEL` for reliable model attribution. No model name is guessed when missing. The compatibility `session_model_usage` table contains real Watson invocation counts; it is not a claim that Watson runs Hermes. The client runs with a separate home so personal Codex histories are not reported as Watson work. Do not configure an external agentsview index in that isolated home.

Set `agent_index_id` and `agent_repository_url` in private configuration, then:

```sh
.venv/bin/watson --home .watson index --register
.venv/bin/watson --home .watson index --dry-run
.venv/bin/watson --home .watson index
```

Registration publishes page metadata; reporting publishes token counts, not prompts, issue text, code, credentials or file paths. Verification is a separate manual process by the event team. Registration does not imply verification.

## Verification

```sh
python -m unittest discover -s tests -v
python watson/vendor/agent_index_client.py --self-check
```

The tests cover persistence, cache invalidation, reply/resume, private access, browser profile restrictions, recipient checks, unknown delivery deduplication, GitHub write scope, no merge, and measured usage. External actions are mocked in unit tests. The real end-to-end pilot uses a separate private repository with a fictional login app.

## Limitations

Polling requires the machine and credentials to be available. The CLI is currently macOS/Linux (file locks); Windows needs an adapter. Issues/code are untrusted data. Model results are proposals; valid citation IDs do not prove semantic correctness. Source collection avoids common secret filenames but is not a secret scanner. Do not send private customer data to public galleries.

A crash between persisting workflow state and sending a notification can leave an incomplete action for human review; uncertain writes are not automatically replayed. Browser scenarios and repair runners need owner configuration. Native voice delivery remains blocked upstream.

MIT for Watson; bundled third-party client retains its own license.
