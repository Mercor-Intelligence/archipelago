"""Pydantic models for code execution."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CodeExecRequest(BaseModel):
    """Request model for code execution."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["help", "exec"] = Field(
        "exec", description="Action: 'help' for usage info, 'exec' to run code"
    )
    code: str | None = Field(
        None,
        description=(
            "Shell command to execute. This runs in bash, NOT a Python interpreter. "
            "Examples:\n"
            "• Simple Python: python -c 'print(1+1)'\n"
            "• Multi-line Python: Write file first, then run:\n"
            "  cat > script.py << 'EOF'\n"
            "  import pandas\n"
            "  print(pandas.__version__)\n"
            "  EOF && python script.py\n"
            "• Shell commands: ls -la, echo hello, etc."
        ),
    )


class ActionInfo(BaseModel):
    """Information about an action."""

    model_config = ConfigDict(extra="forbid")
    description: str
    required_params: list[str]
    optional_params: list[str]


class HelpResponse(BaseModel):
    """Help response listing available actions."""

    model_config = ConfigDict(extra="forbid")
    tool_name: str
    description: str
    actions: dict[str, ActionInfo]


class CodeExecResponse(BaseModel):
    """Response model for code execution."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="Action that was performed")
    output: str | None = Field(None, description="Output from code execution")
    success: bool = Field(..., description="Whether execution succeeded")
    error: str | None = Field(None, description="Error message if execution failed")
    help: HelpResponse | None = Field(None, description="Help info when action=help")
