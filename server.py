#!/usr/bin/env python3
"""
Talent Loop — AI Recruitment OS (runnable slice)

    python3 server.py

No pip install, no network access required — standard library only.
See README.md for architecture, demo logins, and what's real vs.
integration-ready.
"""
import os
import socketserver
from app.db import run_migrations, seed_if_empty
from app.routes import Handler

PORT = int(os.environ.get("PORT", 8000))


class TalentLoopServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    applied = run_migrations()
    if applied:
        print(f"Applied migrations: {', '.join(applied)}")
    if seed_if_empty():
        print("Seeded demo data (organization, users, jobs, candidates, campaigns, 21 days of metrics history).")
    with TalentLoopServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Talent Loop running -> http://localhost:{PORT}")
        print("Demo logins - admin/admin123, recruiter/recruiter123, viewer/viewer123")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
