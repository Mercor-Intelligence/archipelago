"""
Models for helper definitions.
"""

from enum import StrEnum


class HelperIds(StrEnum):
    TEMPLATE = "template"
    SNAPSHOT_DIFF = "snapshot_diff"
    FINAL_ANSWER = "final_answer"
