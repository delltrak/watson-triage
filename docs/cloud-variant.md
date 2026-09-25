# The cloud variant

An image built `FROM` the pinned Plow base that runs Watson's own `cycle`
unattended on a tenant VM. Same collector, same SQLite state, same resumable
round — what changes is where the thinking happens and where the credentials
come from.

## What is different from the local install

**Inference is Plow's lane, not a Codex subscription.** `analysis.PlowInference`
calls `${PLOW_API_BASE}/v1/chat/completions` with the bearer `plow-init`
publishes as `HERMES_CUSTOM_PLOW_API_KEY`. **The base supplies the endpoint and
the credential; Watson chooses its own model** — `z-ai/glm-5.2` unless
`init --model` says otherwise. Changing the base's `config.yaml` seed moves the
Hermes gateway's model, not Watson's. There is no `codex login` and no ChatGPT account, and
this is the only backend — the local path uses it too, against a credential
minted by `plow-agents mint`.

**Browser validation and audio are dormant, not removed.** Both need a desktop:
Playwright drives a real Chromium, and the voice path reads a ChatGPT session
out of `~/.codex/auth.json`. The container configures no `browser_profiles`, so
`cycle` never reaches `run_browser`, and it never generates audio on its own —
`voice` and `deliver --audio` are explicit commands. Run Watson on your Mac when
you want the recorded evidence.

**The owner connects GitHub in chat, and the agent never holds the tokens.**
[docs/INSTALL.md](INSTALL.md#1-connect-github-in-chat) owns the owner's side.
The mechanism is the Watson Triage GitHub App's device flow, split by uid:

- The chat, as the agent, runs `watson github connect`. That only creates an
  empty request file in the agent's home, which root `lstat`s and unlinks but
  never opens, and reads the answer from `/run/watson-github/status.json`,
  which root writes and the agent can only read: a state, the login, the
  install URL, the user code and verification URI while one is pending, and
  once connected the repositories the owner can pick from (name, whether
  private, last push; ten at most, and how many there are). `watson github
  disconnect` asks the same way, with a second request file.
- Root's half, `python3 -I -m watson.githubapp`, runs inside the `watson-cycle`
  service, between passes. It asks GitHub for a device code, polls while the
  owner approves it, and keeps the access and refresh tokens in
  `/var/lib/watson-github/token.json`, `0600` in a root `0700` directory the
  agent cannot enter. The device code and the refresh token never leave it.
  The image builds in the app's public client id; there is no client secret.
  Whenever something changed -- a connect, a refresh, a status lost to a
  restart, the owner asking to connect again after installing the app -- it
  reads the login and lists the repositories: `GET /user/installations`, then
  one page of `/user/installations/{id}/repositories` for each of the first
  ten, which with a user token covers only what the app is installed on and
  the owner can read. A list GitHub will not give is left out, which is not an
  empty one: the connection stands, root asks again on the next tick, and
  before `init` the owner is told nothing about repositories until it has one. On the owner's disconnect it deletes `token.json` and sends both
  tokens to GitHub's credential revocation endpoint (`POST /credentials/revoke`,
  unauthenticated by design, for `ghu_` and `ghr_` tokens alike), which GitHub
  may refuse or rate-limit without changing the outcome here: the tokens are
  gone either way, with no copy to retry with, so the status says whether GitHub
  took the revocation (`revoked`) and the owner is told to revoke it themselves
  when it did not. It then publishes `disconnected` with detail `by_owner`. A
  marker in the store keeps that across restarts until the next connection, and
  `by_owner: true` rides on every state the status takes meanwhile, so the
  pass, which cannot read the marker, sends no refusal notice for the 401 it
  gets, even after a reconnect the owner abandoned or denied.
- The access token lasts eight hours. Root refreshes it between passes once
  less than two hours are left -- never under a pass, since a refresh retires
  the old pair at once -- and hands each pass the access token alone, as
  `GH_TOKEN`. Nothing puts it in the environment s6 publishes: the gateway
  shares this container and has a shell, and anything there is one `printenv`
  from the model.
- The owner hears that GitHub is connected without asking. Root dates a
  connection when the owner approves the code, keeps the date through
  refreshes, and publishes it. A new one ends the pause between passes, which
  otherwise returns only at the tick, so asking again buys no pass. The service
  then runs `watson github announce` as the agent, with no token: from the
  status, it tells the owner which repositories they can pick from, the install
  link, or on a configured install only that GitHub is reconnected (whether
  the app can still read the repository is for the next pass to find), once
  per login and date (`claim_action`), in NOTICE text and validated names only.
  Before `init` there is no `notify_owner` to ask and the owner is in the chat
  setting up, so it goes out regardless; after, `notify_owner` decides. The
  language is `init`'s, or before it the one `github connect --language` left
  in `connect.json` in the agent's home. Root never writes that home: a
  root-owned file beside the agent's database would lock the agent out of it.

The service tests the store by doing, not by reading its mode: if the agent's
uid can enter `/var/lib/watson-github` -- a host directory mounted over it, say,
and Docker Desktop on macOS enforces no mode there -- it refuses to run, before
anything is written there. Compose keeps the store in a named volume, which
takes the image's `root 0700` on first mount, macOS included.

The pass runs with `HOME` set to an empty directory root owns, not the agent's
home. `gh` reads its config from `$HOME/.config/gh`, and a config the agent
wrote there (an `http_unix_socket`) would hand the token to a socket the agent
listens on, every pass.

The residuals this page will not overstate away:

- The pass runs as `hermes`, the gateway's own uid, so its `/proc/<pid>/environ`
  -- `GH_TOKEN` with it -- is readable for as long as a pass runs: minutes when
  an issue changed, since triaging it waits on inference. What that exposes is
  the access token alone: read-only, good for up to eight hours, on what both
  the owner and the app can reach. Closing it needs a separate uid for the
  cycle (srosro/watson-triage#4), which moves `watson init` out of chat setup
  and into the service -- a product change rather than a hardening pass.
- Device-code phishing. The chat cannot read the tokens, but a prompt-injected
  chat can start a device flow of its own -- with the same public client id, or
  with `gh auth login` and gh's own OAuth app, which asks for write scopes --
  and ask the owner to type the code; the token then lands in the model's
  context. An issue update's model-written summary can carry a stranger's
  Watson Triage code too, prompt-injected through an issue comment, and
  GitHub's page then looks exactly right. Nothing here closes that in code,
  since `gh` and `curl` are in the image for the cycle's own use. The owner
  does: a code is approved only right after asking Watson to connect, and only
  when the page names Watson Triage. The name cannot tell a stranger's code
  from the owner's, since both use the same client id, so the timing is the
  check that holds. The skill and INSTALL.md both say so.

Two things on the hosted path are not verified yet: that its `PLOW_API_BASE` is
`https`, which every Plow call here requires, and that `/var/lib/watson-github`
survives a `plow-agents deploy` of a new image. If it does not, the owner
connects again after an update; the store does not move into the agent's home
to avoid that.

Shutdown is not clean yet either (srosro/watson-triage#9). On SIGTERM the
service starts no new pass, but the running one is never told to stop: it keeps
starting new issues until it ends or the container's grace period SIGKILLs it,
and a send killed that way stays `sending` rather than `unknown`.

## What this repository must not own

The base owns these, and a copy here is a second owner that goes stale silently:

| fact | owner |
|---|---|
| the `plow_chat` plugin SHA | `plow-hermes-agent`'s `ARG PLOW_CHAT_PLUGIN_SHA` |
| boot, identity, the dotenv, `SOUL.md` composition | `plow-init` |
| `PLOW_API_BASE`, `PLOW_AGENT_TOKEN`, `HERMES_CUSTOM_PLOW_API_KEY`, `AGENT_ID` | `plow-init`, published to the container environment |
| the *gateway's* inference provider, model and vision lane | the base's `config.yaml` seed |
| the Agent Index reporter and its pinned client | the base's `agent-index` service and `vendor/client.pin` |

This repository owns the persona, the `watson-setup` skill, the `watson-cycle`
service, and Watson itself. That is the whole list.
