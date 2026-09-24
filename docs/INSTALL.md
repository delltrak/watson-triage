# Install Watson on Plow

Watson runs as a cloud agent on a Plow phone line. You text it; it reads the
GitHub issues assigned to you and reports what it actually found.

## What you need

A Plow account with a free line, and a GitHub fine-grained personal access token
for the repository you want watched. The supported path builds the image on your
machine, so Docker and a clone of this repository too. No ChatGPT or Codex
account — the agent thinks through Plow's own inference.

## 1. Give it a GitHub token — at deploy time, not in chat

**Watson will not accept a token you text it, and will refuse if you offer.** A
token in the conversation is in the model's context and therefore at the
inference provider; that is a repository credential handed to a third party, and
nothing downstream can undo it.

Create a **fine-grained PAT** scoped to the repository, with **Issues: read**,
**Contents: read** and **Actions: read** — read-only throughout, because this
deployment triages and never repairs. Create one at
<https://github.com/settings/personal-access-tokens/new>.

Running locally with compose, put the **raw token** in `/etc/watson/github` —
no `GH_TOKEN=` prefix, nothing else in the file — where compose bind-mounts it
from:

```sh
sudo install -d -m 700 -o root -g root /etc/watson
sudo install -m 600 -o root -g root /dev/null /etc/watson/github
sudo sh -c 'cat > /etc/watson/github'   # paste the token, then Ctrl-D
```

Three things about that, each closing a way the first draft of these
instructions leaked:

- **The token is typed into stdin, never into the command.** A token in the
  command line lands in your shell history and in the process table.
- **The destination is created empty and `0600` first.** A redirect into an
  existing file keeps that file's mode, so a stale permissive
  `/etc/watson/github` would quietly take the new token — `install` replaces it
  rather than writing through it.
- **It lives under a root-owned `0700` directory**, created before anything is
  written into it. Writing straight to `./watson-github` let anyone who can
  write that checkout swap it for a symlink between the create and the write —
  which, with `sudo`, means disclosing the token or truncating whatever the
  symlink pointed at.
- **`root:root` is not optional.** A bind mount carries the **host's**
  ownership, so a file owned by your own account can land on uid 10000 inside
  the container — the agent itself, the one identity that must not have it. The
  cycle refuses to run when it detects that.

It is bind-mounted read-only into the container, which is why it is a bare
value rather than an env file: it never enters the container environment, where
the agent's own shell could read it.

**The cycle checks this by doing it, and refuses to run if it fails.** On every
pass it tries to read the token as uid 10000; if that succeeds it stops and
tells you why, rather than triaging attacker-written issue text with a
credential the model can reach.

Two ordinary situations trip it with `stat` still reporting `root:root 0600`:
**Docker Desktop on macOS** does not enforce mounted file modes at all — there
is no fix there, run the agent on a Linux host — and a **Linux host whose own
uid is 10000** maps the file onto `hermes`, where `0600` is precisely the wrong
owner.

On the hosted path, `plow-agents deploy` injects only the `PLOW_*` variables, so
there is no hook for this yet and the agent stands down with "no GH_TOKEN"
until something like GitHub device authorization lands. **Until then, the hosted path is deploy-only — run Watson under
compose if you want it working today.**

## 2. Deploy

```sh
git clone https://github.com/plow-pbc/plow-agents.git
export PATH="$PWD/plow-agents/bin:$PATH"
git clone https://github.com/delltrak/watson-triage.git
cd watson-triage           # --local builds THIS checkout; compose reads ./compose.yml
plow-agents login          # text the activation phrase to the number it prints
plow-agents lines          # keep the ID of a line reported `free`
plow-agents deploy --local --line ln_xxx
plow-agents agents         # until the status is `running`
```

`--local` builds the image here and brings compose up, which is why it runs
from this checkout and after section 1: the token mount is declared with
`create_host_path: false`, so a missing `/etc/watson/github` refuses to start
rather than inventing an empty directory in its place.

The hosted path takes a published digest instead -- `plow-agents image show
watson-delltrak --jq .plow.image` is a public read that prints the reference
Plow currently pins -- and needs no clone and no Docker. It also cannot supply
a GitHub credential yet, so the agent stands down. Do not start there expecting
a working agent.

## 3. Text it

Text the number `plow-agents lines` showed. Watson asks for two things:

- **the repository** — `owner/repo`
- **the assignee** — the GitHub login whose assigned issues are yours. This is
  the whole selection rule, so a wrong login means Watson sees nothing rather
  than too much.

It does not check the token — it has no access to one, deliberately. The first
supervised cycle is what proves it, and an auth failure surfaces there.

## 4. What happens next

The first sync records a baseline and deliberately does **not** work through
your backlog. Issues assigned to you from then on are picked up on their own,
every ten minutes. To have Watson look at something already open, tell it the
number.

Watson never merges, and this deployment never repairs either — `repair` needs
a checkout and a container to run the regression test in, neither of which the
cloud agent has. Run it on your own machine for that.

## Running it on your own machine instead

The same `cycle` runs locally — see the repository README. The local path still
needs the `plow-agents` CLI on your `PATH` (cloned as in step 1), `gh auth login`,
and a Plow credential for inference (`plow-agents mint`),
and it is the only path where browser validation and audio work, because both
need a desktop.
