"""MCP tool implementations for Greenhouse server.

Contains all MCP tool functions organized by domain:
- candidates: Candidate management tools
- applications: Application lifecycle tools
- jobs: Job and pipeline management tools
- feedback: Scorecard and feedback tools
- activity: Activity feed tools
- users: User management tools
- jobboard: Public job board tools
- admin: Administrative tools (reset_state, export_snapshot)

Note: Authentication (login_tool) and server_info are provided by the shared
mcp_auth and mcp_middleware packages respectively.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports when loaded externally (e.g., UI generator)
_parent = str(Path(__file__).parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from tools.activity import register_activity_tools  # noqa: E402
from tools.admin import register_admin_tools  # noqa: E402
from tools.applications import register_application_tools  # noqa: E402
from tools.candidates import register_candidate_tools  # noqa: E402
from tools.feedback import register_feedback_tools  # noqa: E402
from tools.jobboard import register_jobboard_tools  # noqa: E402
from tools.jobs import register_job_tools  # noqa: E402
from tools.lookups import register_lookup_tools  # noqa: E402
from tools.users import register_user_tools  # noqa: E402

__all__ = [
    "register_activity_tools",
    "register_admin_tools",
    "register_application_tools",
    "register_candidate_tools",
    "register_feedback_tools",
    "register_jobboard_tools",
    "register_job_tools",
    "register_lookup_tools",
    "register_user_tools",
]
