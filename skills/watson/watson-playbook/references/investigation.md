# Investigating an issue

1. Call `watson_investigate` with `issue` = the link (or `#N`, `owner/repo#N`)
   and `language` = `pt` or `en` matching the user. A repository homepage
   without `/issues/N` is not an issue: ask for the issue link or number.
2. Reply order, top to bottom:
   - `speak_first` exactly, when present (a linked PR already delivered or is
     open — never ask "which PR is it?"). It already carries `repo_note`.
   - Otherwise `repo_note` exactly, when present (which repo a bare `#N` went to).
   - A short summary: what is going on (outcome first), the evidence behind it,
     then what a human should do next. Use only `result.findings`; say
     "hipótese" / "hypothesis" when certainty is `hypothesis`.
   - `review_note` exactly, as the last line.
3. Keep it short for iMessage: plain sentences, `-` bullets at most, **bold**
   for emphasis, no backticks, no tables, no code blocks.
4. A merge is not a release: say "mergeado na main", never "publicado".
5. If the tool returns an error, relay it in the user's language (bilingual
   errors come as `EN --- PT`: pick the matching half).
6. For "that issue from yesterday", `session_search` helps find the link.
