"""
Inspect AI implementation of Archipelago's simple_task.

Task: Find the gorilla image in a filesystem with multiple subdirectories.

Usage:
    inspect eval simple_task.py --model openai/gpt-4o
"""

import os
import mimetypes
from pathlib import Path
from inspect_ai import Task, task, eval
from inspect_ai.dataset import Sample
from inspect_ai.solver import TaskState, Generate, generate
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.tool import tool
from inspect_ai.model import ChatMessageUser, ContentImage, user_prompt
from inspect_ai.agent import Agent, AgentState, agent, react, sandbox_agent_bridge
from inspect_ai.util import sandbox


# ============================================================================
# FILESYSTEM TOOLS
# ============================================================================

def _resolve_path(path: str) -> str:
    """Resolve a path relative to sandbox working directory."""
    # Sandbox working directory is /tmp/sandbox (set in Dockerfile)
    # Files from Sample.files are copied there
    sandbox_wd = "/tmp/sandbox"
    
    if not path or path == "/":
        return sandbox_wd
    # Strip leading slash and join with sandbox root
    rel = os.path.normpath(path).lstrip(os.sep)
    return os.path.join(sandbox_wd, rel)


@tool
def list_files():
    async def execute(path: str = "/") -> str:
        """
        List files and folders in the given path.
        
        Args:
            path: Directory path to list (relative to sandbox root). Default: '/' (sandbox root). Example: /animals
        
        Returns:
            String listing of files and folders with their types and sizes.
        """
        resolved_path = _resolve_path(path)
        
        # Use sandbox().exec() to run ls command in the container
        result = await sandbox().exec(["ls", "-lah", resolved_path])
        
        if result.success:
            return result.stdout
        else:
            # Handle common errors
            stderr = result.stderr.lower()
            if "no such file" in stderr:
                return f"[not found: {path}]"
            elif "permission denied" in stderr:
                return f"[permission denied: {path}]"
            elif "not a directory" in stderr:
                return f"[not a directory: {path}]"
            else:
                return f"[error: {result.stderr}]"
    
    return execute


@tool
def read_image_file():
    async def execute(file_path: str) -> ContentImage:
        """
        Read an image file (png, jpeg, gif, webp) from the filesystem.
        
        Args:
            file_path: Path to the image file. REQUIRED. Must start with /. Example: /animals/xk92m/qz7fw.png
        
        Returns:
            Image content that the vision model can view.
        """
        if not isinstance(file_path, str) or not file_path:
            raise ValueError("File path is required and must be a string")

        if not file_path.startswith("/"):
            raise ValueError("File path must start with /")

        # Validate file extension
        file_ext = file_path.lower().split(".")[-1]
        if file_ext not in ("png", "jpg", "jpeg", "gif", "webp"):
            raise ValueError(
                f"Unsupported image format: {file_ext}. Supported formats: png, jpg, jpeg, gif, webp"
            )

        resolved_path = _resolve_path(file_path)

        try:
            # Use sandbox().read_file() to read the image from the container
            image_data = await sandbox().read_file(resolved_path, text=False)

            # Determine image format
            image_format = {
                "png": "png",
                "jpg": "jpeg",
                "jpeg": "jpeg",
                "gif": "gif",
                "webp": "webp",
            }[file_ext]

            # Return as ContentImage for vision model
            import base64
            data_url = f"data:image/{image_format};base64,{base64.b64encode(image_data).decode()}"
            return ContentImage(image=data_url)

        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {file_path}")
        except PermissionError:
            raise PermissionError(f"Permission denied: {file_path}")
        except Exception as exc:
            raise RuntimeError(f"Failed to read image file: {repr(exc)}") from exc
    
    return execute


@tool
def get_directory_tree():
    async def execute(
        path: str = "/",
        max_depth: int = 3,
        include_files: bool = True
    ) -> str:
        """
        Display a directory tree structure with ASCII tree visualization.
        
        Args:
            path: Directory path to display tree for. Default: '/' (root). Example: /animals
            max_depth: Maximum depth to traverse. Default: 3. Range: 1-10.
            include_files: Include files in the tree, not just directories. Default: True
        
        Returns:
            ASCII tree visualization of the directory structure.
        """
        # Validate and clamp max_depth
        if max_depth < 1:
            max_depth = 1
        elif max_depth > 10:
            max_depth = 10

        if not isinstance(path, str) or not path:
            raise ValueError("Path is required and must be a string")

        if not path.startswith("/"):
            raise ValueError("Path must start with /")

        resolved_path = _resolve_path(path)
        
        # Use tree command if available, otherwise use find
        tree_args = ["tree", "-L", str(max_depth)]
        if not include_files:
            tree_args.append("-d")
        tree_args.append(resolved_path)
        
        result = await sandbox().exec(tree_args)
        
        if result.success:
            return result.stdout
        else:
            # Fallback to find command if tree not available
            find_args = ["find", resolved_path, "-maxdepth", str(max_depth)]
            if not include_files:
                find_args.extend(["-type", "d"])
            
            result = await sandbox().exec(find_args)
            if result.success:
                return result.stdout
            else:
                stderr = result.stderr.lower()
                if "no such file" in stderr:
                    return f"[not found: {path}]"
                elif "not a directory" in stderr:
                    return f"[not a directory: {path}]"
                else:
                    return f"[error: {result.stderr}]"
    
    return execute


# ============================================================================
# DATASET
# ============================================================================

def create_dataset() -> list[Sample]:
    """
    Create dataset with initial filesystem files.
    
    Maps local animal images to sandbox filesystem paths.
    Files will be copied to /tmp/sandbox/animals/ in the sandbox.
    """
    data_dir = Path(__file__).parent / "data"
    animals_dir = data_dir / "animals"
    
    # Build file mapping for Sample.files
    files = {}
    for subdir in animals_dir.iterdir():
        if subdir.is_dir():
            for img_file in subdir.glob("*.png"):
                # Path in sandbox: /tmp/sandbox/animals/xk92m/qz7fw.png
                rel_path = f"animals/{subdir.name}/{img_file.name}"
                files[rel_path] = str(img_file)
    
    return [
        Sample(
            input=(
                "There are several subdirectories in this filesystem, "
                "each containing an image of an animal. Read each image "
                "file to view its contents, find the gorilla, and tell "
                "me its full path."
            ),
            target="/animals/xk92m/qz7fw.png",
            files=files,
            metadata={
                "task_id": "example_task_001",
                "description": "Find the gorilla image in filesystem"
            }
        )
    ]


# ============================================================================
# SCORER
# ============================================================================

@scorer(metrics=[accuracy()])
def find_gorilla_scorer():
    """
    Check if agent found the correct gorilla path using pattern matching.
    
    Looks for the expected path in the agent's final answer.
    """
    
    async def score(state: TaskState, target: Target) -> Score:
        # Extract final answer from agent's last message
        final_message = state.messages[-1] if state.messages else None
        answer = final_message.content if final_message else ""
        
        # Expected path
        expected_path = target.text
        
        # Pattern matching - check if expected path appears in answer
        # Handle both "/animals/..." and "animals/..." formats
        correct = (
            expected_path in answer or 
            expected_path.lstrip('/') in answer
        )
        
        return Score(
            value="C" if correct else "I",
            answer=answer,
            explanation=(
                f"Expected path '{expected_path}' "
                f"{'found' if correct else 'not found'} in final answer"
            )
        )
    
    return score


# ============================================================================
# AGENTS
# ============================================================================

@agent
def claude_code_agent() -> Agent:
    """
    Agent that uses Claude Code CLI running in the sandbox.
    
    Claude Code runs as a sandboxed process and makes API calls through
    the sandbox_agent_bridge, which redirects them to Inspect's model provider.
    """
    async def execute(state: AgentState) -> AgentState:
        # Use sandbox bridge to redirect Anthropic API calls to Inspect's model
        async with sandbox_agent_bridge(state) as bridge:
            
            # Get the user's task/prompt
            prompt = user_prompt(state.messages)
            
            # Run Claude Code in the sandbox
            result = await sandbox().exec(
                cmd=[
                    "claudecode",
                    "--prompt",
                    prompt.text,
                    "--non-interactive"
                ],
                env={
                    "ANTHROPIC_BASE_URL": f"http://localhost:{bridge.port}",
                    "ANTHROPIC_API_KEY": "bridge"  # Placeholder, not used
                }
            )
            
            if not result.success:
                raise RuntimeError(f"Claude Code error: {result.stderr}")
            
            # Bridge automatically tracks state changes from API calls
            return bridge.state

    return execute


# ============================================================================
# TASK DEFINITION
# ============================================================================

@task
def simple_task(agent_type: str = "react"):
    """
    Find the gorilla in a filesystem with multiple animal images.
    
    This is an Inspect AI implementation of Archipelago's simple_task example.
    
    Args:
        agent_type: Which agent to use - "react" (default) or "claude_code"
    
    The task tests:
    - Filesystem navigation (via Inspect tools or Claude Code's shell access)
    - Image viewing with vision model
    - Systematic exploration and reporting
    
    Usage:
        # With React agent (uses Inspect tools)
        inspect eval simple_task.py --model openai/gpt-4o
        
        # With Claude Code agent (uses shell commands)
        inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0
    """
    
    # Choose solver based on agent_type
    if agent_type == "claude_code":
        # Claude Code agent - uses shell commands in sandbox
        solver = claude_code_agent()
        
        # System prompt for Claude Code (it has full shell access)
        # Note: This is set via the Sample input, not here
        
    elif agent_type == "react":
        # React agent - uses Inspect tools
        system_prompt = """You are an AI assistant with vision capabilities that completes tasks using tools.

Think step-by-step:
1. Use list_files() to explore directories and find subdirectories with images
2. Use read_image_file() to view each image - you will see the actual image content
3. Identify which image shows a gorilla
4. Report its full path (e.g., /animals/subfolder/image.png)

Be systematic and thorough. The filesystem starts at / (sandbox root).

Available tools:
- list_files(path): List files and directories at the given path
- read_image_file(path): Read and view an image file (you will see the image)
- get_directory_tree(path, max_depth, include_files): Get directory structure as ASCII tree"""
        
        # Create tool instances
        tools = [list_files(), read_image_file(), get_directory_tree()]
        
        solver = react(
            prompt=system_prompt,
            tools=tools,
        )
    else:
        raise ValueError(f"Unknown agent_type: {agent_type}. Must be 'react' or 'claude_code'")
    
    return Task(
        dataset=create_dataset(),
        solver=solver,
        scorer=find_gorilla_scorer(),
        sandbox="docker",
        name="simple_task"
    )


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    # Run evaluation locally
    results = eval(
        simple_task(),
        model="openai/gpt-4o",
        log_dir="./logs"
    )
    
    # Display results
    print(f"\n{'='*60}")
    print(f"Simple Task Results:")
    print(f"  Accuracy: {results[0].metrics['accuracy']:.2f}")
    print(f"  Status: {results[0].status}")
    print(f"  Samples: {len(results[0].samples)}")
    print(f"{'='*60}\n")
