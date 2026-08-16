# agent-workspace

Public GitHub Actions workspace for short-lived SSH worker runners used by sshworker.

The workflows start Linux/macOS/Windows runners, expose SSH and Worker Agents through
`*.agentsweb.space`, and provision short-lived worker runtimes on the runner. Worker
Agents is checked out at workflow runtime from https://github.com/replypaldevs/workerAgents.
