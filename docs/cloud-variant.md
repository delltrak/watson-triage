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

**The owner's GitHub token never enters the container's long-lived environment.**
[docs/INSTALL.md](INSTALL.md#1-give-it-a-github-token--at-deploy-time-not-in-chat)
owns the operator contract. The mechanism is a read-only bind mount: the raw
token sits at `/opt/plow/watson-github`, owned `root:root 0600`, and the cycle
service reads it as root before dropping privileges. Nothing puts it in the environment s6 publishes, so nothing has to remember to
take it out — the gateway shares this container and has a shell, and anything
there is one `printenv` from the model. It does reach one environment: the
cycle's own child, for the seconds a pass runs, which is the residual below.

A bind mount's permissions are the **host's**, so the cycle does not trust the
reported mode: every pass tries to read the file as uid 10000 and **refuses to
run** if that succeeds. Two ordinary setups trip it — Docker Desktop on macOS,
which does not enforce mounted modes at all, and a Linux host whose own uid is
10000, where `0600` lands on `hermes` itself.

The residual this page will not overstate away: the cycle hands the token to a
child running as `hermes`, the gateway's own uid, so `/proc/<pid>/environ` is
readable for the seconds a pass takes. Closing that needs a separate uid for the
cycle, which moves `watson init` out of chat setup and into the service -- a
product change rather than a hardening pass.

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
