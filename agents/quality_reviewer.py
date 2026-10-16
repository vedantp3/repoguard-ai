from agents.roles import INSTRUCTIONS, TOOLS
from agents.specialist import Specialist


class QualityReviewer(Specialist):
    """Finds maintainability and reliability risks."""

    instructions = INSTRUCTIONS["quality_reviewer"]
    allowed_tools = TOOLS["quality_reviewer"]

    def __init__(self, provider):
        super().__init__(provider, "quality_reviewer")
