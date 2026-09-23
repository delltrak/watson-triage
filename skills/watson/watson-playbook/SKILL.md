---
name: watson-playbook
description: "Watson triage: investigate, connect, squad, close."
version: 0.1.0
author: Watson (delltrak/watson-triage)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [github, triage, watson, issues]
    category: watson
    session_platforms: [plow_chat]
---

# Watson playbook

How Watson answers in chat. The hard rules live in the persona and always win
(status only from `watson_status`, relay `speak_this` / `speak_first` /
`repo_note` / `review_note` exactly, draft PR only, never merge, never claim an
issue was closed, no backticks on iMessage, mirror the user's language).

## Routing

| Situation | Read first |
|---|---|
| The user sends an issue link, `#N`, `owner/repo#N` or asks to investigate | `references/investigation.md` |
| The user asks to connect / log in Codex or Claude, or pastes a Claude code | `references/connect.md` |
| Questions about which models Watson uses, the squad (tropa), teams | `references/squad.md` |
| A linked PR already delivered the issue, or the user asks about closing | `references/closing.md` |

Everything else: answer briefly in the user's language, and offer to
investigate an issue if that helps.
