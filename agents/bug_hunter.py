from agents.roles import INSTRUCTIONS, TOOLS
from agents.specialist import Specialist


class BugHunter(Specialist):
    """Finds logic errors, edge cases, and likely runtime failures."""

    instructions = INSTRUCTIONS["bug_hunter"]
    allowed_tools = TOOLS["bug_hunter"]

    def __init__(self, provider):
        super().__init__(provider, "bug_hunter")
