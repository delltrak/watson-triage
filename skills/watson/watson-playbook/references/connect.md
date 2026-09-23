# Connecting Codex or Claude over chat

- Codex: call `watson_connect_codex`. Paste the `https` auth URL plainly so
  iMessage makes it tappable, plus the one-time code if there is one.
- Claude: call `watson_connect_claude`, paste the URL; after the user signs in
  they paste the browser code back — call the tool again with `code`.
- Tell the user Watson pings automatically when login completes; they do not
  need to say "pronto" / "ready".
- Never say it is connected until `watson_status` shows it, or the automatic
  ping went out. `cancel=true` aborts a pending login.
