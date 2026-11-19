# Security Policy

## Local Secret Handling

Antigravity Hub is designed to run locally on `127.0.0.1`:

- **No secret extraction**: `/api/inventory` inspects `.env.example`, `.env`, and `SKILL.md` for key names and returns strictly boolean `configured: true | false` status flags. Existing secret values are never read back to the browser UI.
- **Local `.env` storage**: Keys submitted in the **API Keys** modal are written only to your local `.env` files (which are excluded via `.gitignore`) and injected directly into child process environments on launch.
- **Zero telemetry**: No analytics, crash reports, or workspace paths are transmitted externally.

## Reporting a Vulnerability

If you discover a security issue (such as unintended path traversal, command injection, or credential exposure), please report it privately using **GitHub Security Advisories** on this repository rather than opening a public issue.
