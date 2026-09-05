"""
Security: token sessions, RBAC role check, sliding-window rate limiting,
and the tool-permission registry the agent layer enforces independently
of anything the "AI" decides (Section 93: never trust the model to
enforce authorization — the backend checks every call).
"""
import time
import collections
from app.db import hash_password

ROLE_RANK = {"viewer": 0, "recruiter": 1, "team_lead": 2, "admin": 3}

SESSIONS = {}  # token -> {"username", "role", "organization_id"}

# tool_name -> minimum role required to execute it. Read-only tools are
# open to viewers; anything that writes needs recruiter+; anything that
# reaches outside the system (handled via workflow approvals) is gated
# further at the workflow layer regardless of role.
TOOL_PERMISSIONS = {
    "searchCandidates": "viewer",
    "matchCandidates": "viewer",
    "getRecruitmentStats": "viewer",
    "findBottlenecks": "viewer",
    "rediscoverCandidates": "viewer",
    "findNeverHired": "viewer",
    "generateDailyBriefing": "viewer",
    "createScreeningQuestions": "recruiter",
    "createCampaign": "recruiter",
    "compareJobs": "viewer",
}

_RATE_WINDOW_SECONDS = 60
_RATE_MAX_REQUESTS = 120
_rate_buckets = collections.defaultdict(list)


def check_rate_limit(key):
    now = time.time()
    bucket = _rate_buckets[key]
    while bucket and bucket[0] < now - _RATE_WINDOW_SECONDS:
        bucket.pop(0)
    if len(bucket) >= _RATE_MAX_REQUESTS:
        return False
    bucket.append(now)
    return True


def require_auth(headers, min_role=None):
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    session = SESSIONS.get(token)
    if not session:
        return None
    if min_role and ROLE_RANK.get(session["role"], -1) < ROLE_RANK.get(min_role, 99):
        return None
    return session


def tool_allowed(session, tool_name):
    required = TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        return False, "unknown tool"
    if ROLE_RANK.get(session["role"], -1) < ROLE_RANK.get(required, 99):
        return False, f"requires {required} role or higher"
    return True, "ok"


def has_role(session, min_role):
    """The one function every route handler should use for role checks —
    rank-based, so a team_lead automatically satisfies anything gated at
    'recruiter', and admin satisfies everything. Avoids the bug class
    where a hand-written `role not in (...)` list quietly excludes a
    higher role that should obviously be allowed."""
    return ROLE_RANK.get(session["role"], -1) >= ROLE_RANK.get(min_role, 99)
