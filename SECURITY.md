# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: use **GitHub → Security → Report a vulnerability**
on this repository (private vulnerability reporting). Include what you found, how to reproduce it and the affected
version (`dridh --version`). You will get an acknowledgement within a week.

## What Dridh protects

- **Human-only approvals.** Every approval (Use Cases, technical design, plan, execution checkpoints, feature
  completion) is a typed, revision-bound confirmation in an interactive terminal. No bridge operation approves, ticks
  a checklist, starts a run, completes a feature or delivers; agents are refused.
- **Isolation.** Each story runs in its own Git worktree; workers cannot write outside it.
- **Credentials.** Dridh never stores provider keys; it reads what OpenCode already has. Logs, reports and evidence are
  redacted (`docs/security/REDACTION.md`), and CI scans every tracked file for credential material.
- **Your OpenCode configuration.** `dridh install` backs up every file it changes and never sets your default agent;
  `dridh uninstall` restores the original.

## Supported versions

Only the latest release receives fixes.
