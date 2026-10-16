from agents.roles import INSTRUCTIONS, TOOLS
from agents.specialist import Specialist


class SecurityAuditor(Specialist):
    """Finds vulnerabilities, unsafe input handling, and secret exposure."""

    instructions = INSTRUCTIONS["security_auditor"]
    allowed_tools = TOOLS["security_auditor"]

    def __init__(self, provider):
        super().__init__(provider, "security_auditor")
