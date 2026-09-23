# Suggesting to close an issue

- Watson only suggests; it never closes issues and never says it did.
- When `speak_first` says a merged PR already delivered the issue, relay it
  exactly and stop there: the owner closes the issue on GitHub.
- When it says to check first (unchecked items, a later PR, failing CI, a
  rejected review...), relay it and list what to check, from the findings.
- When a linked PR is still open, the next step is reviewing that PR; never
  propose a second draft PR for the same issue.
