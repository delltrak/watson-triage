---
name: watson-setup
description: Set up Watson and track issues by number — which repository to watch, which login's assigned issues to read, and which already-open issues to pick up. Trigger when the owner first messages this agent, when they ask Watson to watch a repository, when they ask it to track or look at an issue number, or when the cycle reports that it has no configuration.
allowed-tools: Bash(/opt/hermes/.venv/bin/watson:*)
---

# Watson setup

Two facts make Watson work, and the owner supplies both. Ask one at a time.

**Never ask the owner for a GitHub token, and refuse if they offer one.** A
token they text is in this conversation, which means it is in the model's
context and therefore at the inference provider — a repository credential
disclosed to a third party, and nothing can hold it at arm's length once it has
been said out loud. The token is deploy-time input, set where the container is
started. If it is missing, say so and point at `docs/INSTALL.md`; do not offer
to accept one here as a workaround.

## 1. The repository

Ask which repository to watch, as `owner/repo`. Take one. Related repositories
can be added later; do not ask about them now.

## 2. The assignee

Ask which GitHub login's assigned issues are the owner's. Usually their own.
Watson reads only issues assigned to this login — that is the whole selection
rule, so a wrong login means Watson sees nothing rather than too much.

## 3. Do not go looking for the token

You have no `gh` here, deliberately. The deploy-time credential is held in a
root-only file the cycle service reads, and it is not yours to fetch, echo, or
check — a credential you can reach is a credential that can end up in this
conversation, and from there at the inference provider.

If the owner asks whether the token works, the honest answer is that the first
cycle will say so -- and that you will not be the one who sees it. The cycle
writes its errors to the service log, which you have no tool to read. Whoever
started the container reads them with `docker compose logs agent`. Say that
plainly rather than offering to check; you do not know, and saying otherwise
would be a guess.

## 4. Initialise

```bash
/opt/hermes/.venv/bin/watson --home /var/lib/hermes/watson init \
  --repo OWNER/REPO --assignee LOGIN --delivery text --notify-owner
```

`--notify-owner` is what makes Watson message the owner when an issue moves.
Without it `notify()` returns immediately and every update is dropped in
silence, so leave it on unless the owner asks for a quiet agent.

`init` refuses to run twice. If it says this install is already configured and
the owner wants a different repository, tell them that is a new agent rather
than an edit, and stop.

## 5. Tell them what happens next

Do **not** run `watson sync` yourself — you have no token, by design, and it is
the one setup command that needs one. The supervised cycle runs it on its next
pass, which is also where an unusable token first shows itself.

Say plainly what that means: the first pass records a baseline and deliberately
does **not** work through the backlog. Issues assigned from then on are picked
up on their own, every ten minutes. Anything already open has to be named:

```bash
/opt/hermes/.venv/bin/watson --home /var/lib/hermes/watson track 123
```

`track` is a local record, so it works without a token — the next cycle is what
goes and reads the issue.

Then tell them they will hear from you when an issue actually moves, not on a
schedule. If nothing arrives, an unusable token is the first thing to rule out,
and it shows up in `docker compose logs agent` rather than here -- it is the
deploy-time token, fixed where the agent is started, not something either of
you can correct from this conversation.
