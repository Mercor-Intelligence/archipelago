"""Shared utility functions for mcp_scripts."""

import re


def to_snake_case(name: str) -> str:
    """Convert PascalCase/camelCase/kebab-case to snake_case.

    Examples:
        AccountType -> account_type
        AccountSubType -> account_sub_type
        firstName -> first_name
        IOError -> io_error
        my-kebab-name -> my_kebab_name
    """
    # First, handle spaces and hyphens
    name = name.replace(" ", "_").replace("-", "_")
    # Insert underscore before uppercase letters that follow lowercase letters
    name = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    # Insert underscore before uppercase letters followed by lowercase (for acronyms)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return name.lower()


def to_pascal_case(name: str) -> str:
    """Convert a string to PascalCase.

    Examples:
        my_snake_name -> MySnakeName
        my-kebab-name -> MyKebabName
        some name -> SomeName
    """
    # Replace hyphens and underscores with spaces
    name = name.replace("-", " ").replace("_", " ")
    # Capitalize each word and join them
    return "".join(word.capitalize() for word in name.split())


def to_title_case(name: str) -> str:
    """Convert a string to Title Case (space-separated).

    Examples:
        my_snake_name -> My Snake Name
        my-kebab-name -> My Kebab Name
    """
    # Replace hyphens and underscores with spaces
    name = name.replace("-", " ").replace("_", " ")
    # Capitalize each word
    return " ".join(word.capitalize() for word in name.split())
