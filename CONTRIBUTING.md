# Contributing to Antigravity Hub

Thank you for helping improve Antigravity Hub!

## Local Setup

1. Clone the repository and copy `hub.config.example.json` to `hub.config.json`.
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the launcher:
   ```bash
   python server.py
   ```

## Guidelines

- Keep `server.py` and `ui/index.html` cross-platform across Windows, macOS, and Linux.
- Never commit personal `hub.config.json`, `comments.json`, or `.env` files.
- Keep API key discovery strictly boolean (never return secret values in HTTP responses).
