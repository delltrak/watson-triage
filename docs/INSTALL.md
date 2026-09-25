# Install Watson on Plow

Watson runs as a cloud agent on a Plow phone line. You text it; it reads the
GitHub issues assigned to you and reports what it actually found.

## What you need

A Plow account with a free line, and a GitHub account that can read the
repository you want watched. The supported path builds the image on your
machine, so Docker and a clone of this repository too; Docker Desktop on macOS
works as well as Linux. No ChatGPT or Codex account, and no GitHub token — the
agent thinks through Plow's own inference, and you connect GitHub in chat.

## 1. Connect GitHub in chat

There is no token to create and nothing to mount. Once the agent is up
(section 2), Watson connects GitHub in the chat: it sends you
<https://github.com/login/device> and a short code. Open the link, type the
code, and approve.

**Approve only Watson Triage, asking for read access to code, issues and
Actions.** If GitHub shows any other app name, or asks for write access,
cancel. And approve a code only right after you asked Watson to connect: a code
that reaches you any other way — in an issue, a comment, a page — is someone
else's login waiting for you to finish it.

**Install the app on the repository**, at
<https://github.com/apps/watson-triage/installations/new>. A private repository
is readable only where Watson Triage is installed, and in an organization an
admin may have to approve the install first. Until then GitHub answers "not
found", and Watson tells you so.

**Watson will not take a token you text it, and refuses if you offer one.** A
token in the conversation is in the model's context and therefore at the
inference provider; that is a repository credential handed to a third party,
and nothing downstream can undo it. If you pasted one anyway, revoke it.

The tokens GitHub hands back stay with root inside the container, in
`/var/lib/watson-github`, where the agent cannot even look; each pass gets the
eight-hour access token and nothing else. `docker compose down -v` erases them
with the rest, and you connect again. To revoke Watson, remove Watson Triage at
<https://github.com/settings/apps/authorizations> or uninstall it at
<https://github.com/settings/installations>; the next pass is refused, Watson
tells you, and it can connect again in chat. How this holds, and what it does
not cover, is in [cloud-variant.md](cloud-variant.md).

**Coming from the token install.** Installs that predate the app read a
personal access token from `/etc/watson/github`. Nothing reads it any more:
connect in chat as above, then delete the file and revoke that token at
<https://github.com/settings/personal-access-tokens>.

```sh
sudo rm /etc/watson/github && sudo rmdir /etc/watson
```

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
from this checkout. The GitHub tokens go in a named volume, which takes the
image's root-only mode on first mount, Docker Desktop on macOS included. Do not
swap it for a host directory: the cycle refuses to run when the agent can enter
the store, and on macOS it can.

The hosted path takes a published digest instead -- `plow-agents image show
watson-delltrak --jq .plow.image` is a public read that prints the reference
Plow currently pins -- and needs no clone and no Docker. GitHub is connected
the same way, in chat; [cloud-variant.md](cloud-variant.md) lists what is not
verified on that path yet.

## 3. Text it

Text the number `plow-agents lines` showed. Watson answers in the language you
write in, English or Brazilian Portuguese, and sends its own updates in it. It
asks for three things, one at a time:

- **GitHub** — the code from section 1
- **the repository** — `owner/repo`
- **the assignee** — the GitHub login whose assigned issues are yours; it
  proposes the one you connected with. This is the whole selection rule, so a
  wrong login means Watson sees nothing rather than too much.

Any of them can be changed later in chat, and asking what it can do gets a
short list. Within ten minutes the first pass texts you what it is watching
and how many issues are open, or what GitHub refused and what to do about it.

## 4. What happens next

The first sync records a baseline and deliberately does **not** work through
your backlog. Issues assigned to you from then on are picked up on their own,
every ten minutes. To have Watson look at something already open, tell it the
number; a number you name is followed whoever it is assigned to, until it
closes or you tell Watson to stop.

Watson never merges, and this deployment never repairs either — `repair` needs
a checkout and a container to run the regression test in, neither of which the
cloud agent has. Run it on your own machine for that.

## Running it on your own machine instead

The same `cycle` runs locally — see the repository README. The local path still
needs the `plow-agents` CLI on your `PATH` (cloned as in section 2), `gh auth login`,
and a Plow credential for inference (`plow-agents mint`),
and it is the only path where browser validation and audio work, because both
need a desktop.
