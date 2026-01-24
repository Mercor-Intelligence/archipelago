"""
Constants for snapshot diff utilities.
"""

from enum import StrEnum

# Text file extensions for diff generation
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".scss",
    ".less",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".csv",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".bat",
    ".cmd",
    ".php",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".scala",
    ".clj",
    ".hs",
    ".elm",
    ".dart",
    ".vue",
    ".svelte",
    ".astro",
    ".r",
    ".m",
    ".mm",
    ".pl",
    ".perl",
    ".lua",
    ".nim",
    ".zig",
    ".odin",
    ".v",
    ".cr",
    ".ex",
    ".exs",
    ".erl",
    ".hrl",
    ".fs",
    ".fsx",
    ".ml",
    ".mli",
    ".ocaml",
    ".rkt",
    ".scm",
    ".ss",
    ".lisp",
    ".cl",
    ".asd",
    ".jl",
    ".proto",
    ".thrift",
    ".avro",
    ".graphql",
    ".gql",
    ".dockerfile",
    ".makefile",
    ".cmake",
    ".gradle",
    ".cfg",
    ".ini",
    ".conf",
    ".config",
    ".toml",
    ".lock",
    ".log",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
}

# MIME types for text files
TEXT_MIME_TYPES = {
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/typescript",
    "application/x-sh",
    "application/x-shellscript",
}

# Document file extensions that can be extracted via Reducto
EXTRACTABLE_DOCUMENT_EXTENSIONS = {
    ".docx",
    ".doc",
    ".pdf",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
}

# Concurrency limits
MAX_CONCURRENT_FILE_OPERATIONS = 20

# Pure image file extensions (no text content to extract)
# Limited to formats supported by Google Gemini multimodal API
PURE_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
}

# MIME type mapping for pure image files (fallback when mimetypes.guess_type() fails)
# Must cover all extensions in PURE_IMAGE_EXTENSIONS
PURE_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Validate that all image extensions have MIME type mappings
_missing_mime_types = PURE_IMAGE_EXTENSIONS - set(PURE_IMAGE_MIME_TYPES.keys())
if _missing_mime_types:
    raise ValueError(
        f"PURE_IMAGE_MIME_TYPES is missing entries for: {sorted(_missing_mime_types)}"
    )

# Document file extensions with visual representation (need content extraction for sub-artifacts)
DOCUMENT_WITH_VISUAL_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
}

# All visual file extensions (for backward compatibility)
VISUAL_FILE_EXTENSIONS = PURE_IMAGE_EXTENSIONS | DOCUMENT_WITH_VISUAL_EXTENSIONS

# Multi-part document file extensions (slides, sheets)
MULTI_PART_FILE_EXTENSIONS = {".xlsx", ".xls", ".pptx", ".ppt"}

# Specific file type groups
SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".xlsm"}
PRESENTATION_EXTENSIONS = {".pptx", ".ppt"}
DOCUMENTS_DOCUMENT_EXTENSIONS = {".docx", ".doc"}
PDF_EXTENSIONS = {".pdf"}

# File types that have sub-artifacts from local extraction (sheets for spreadsheets, slides for presentations)
SUB_ARTIFACT_CAPABLE_EXTENSIONS = {".xlsx", ".xls", ".pptx", ".ppt"}

# File types that can be screenshotted for visual grading
SCREENSHOTABLE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}

# File extraction strategy options
#
# Strategies:
#   - LOCAL_WITH_REDUCTO: Two-tier approach for multi-part documents (XLSX, PPTX, DOCX, PDF)
#       * Uses fast local extraction (openpyxl, python-pptx, pypdf) for change detection
#       * Switches to high-quality Reducto extraction only for changed content
#
#   - LOCAL_ONLY (default): Single-tier approach using only local extraction
#       * Uses local extraction for both change detection and full content extraction


class FileExtractionStrategy(StrEnum):
    LOCAL_WITH_REDUCTO = "LOCAL_WITH_REDUCTO"
    LOCAL_ONLY = "LOCAL_ONLY"


# Default file extraction strategy
DEFAULT_FILE_EXTRACTION_STRATEGY = FileExtractionStrategy.LOCAL_WITH_REDUCTO
