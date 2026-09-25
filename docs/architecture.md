# Workflow and trust boundaries

GitHub collector → immutable source/CI evidence → Plow structured triage → persistent case → owner-approved browser scenario → localized comment and owner delivery.

Every issue is identified by repository + number. Case memory records current state, latest run, unresolved questions, previous state, author, source revision and validation evidence. Polling compares conversation, code revision, CI status, access-file revision and browser profile. A reply resumes the existing case; unchanged inputs produce no inference or repeated notification. Watson's own marked comments do not wake itself.

States: waiting_access, waiting_info, triaged, reproduced, validated, blocked, closed. A scenario passing is deliberately not called resolved. Deployment is not inferred from a commit or merge.

Only owner configuration enables comments, delivery or repair. Browser URLs/scenarios and repair file scopes come from that configuration. The model never gets Plow/GitHub credentials or test passwords. Login is unrecorded, in a separate browser context. Trace files stay local; screenshots/videos use test data and are shared only with the configured owner.

The narrow GitHub writer can create comments, new trees/commits/branches and draft PRs. No merge or branch update/delete operation exists. Repairs run separately and require a reproduced issue, scoped changes and a regression test that distinguishes original and fixed source. Unit tests execute in a Docker container without network, host credentials or a writable source mount.

The browser runs locally under the host user, not inside that Docker sandbox. It is restricted to the explicitly configured test origin, but this is not isolation suitable for arbitrary hostile applications. Use a separate OS/container runtime for untrusted deployments. The GitHub pilot remains read-only until project-specific validation/write permissions are configured.

Known interruption behavior: a workflow cursor is saved before side effects to prevent duplicate sends. A crash or transient failure may leave an unsent comment/update requiring operator review. Unknown writes are not automatically retried. No exactly-once delivery guarantee is claimed.
