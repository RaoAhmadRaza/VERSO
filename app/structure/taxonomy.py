"""Closed skill-category enum (PLAN.md §5).

Categories are almost never written in the resume. If the model invents them, the same
skill lands in different buckets on different uploads and everything downstream breaks.
Fixed enum, passed into the prompt, unmatched -> `other` (CLAUDE.md hard rule #7).
"""

from enum import StrEnum


class SkillCategory(StrEnum):
    LANGUAGE = "language"
    FRAMEWORK = "framework"
    LIBRARY = "library"
    DATABASE = "database"
    CLOUD_PLATFORM = "cloud_platform"
    DEVOPS_TOOL = "devops_tool"
    DATA_ML = "data_ml"
    DESIGN_TOOL = "design_tool"
    METHODOLOGY = "methodology"
    DOMAIN_KNOWLEDGE = "domain_knowledge"
    SOFT_SKILL = "soft_skill"
    CERTIFICATION_SKILL = "certification_skill"
    OTHER = "other"


CATEGORY_VALUES = [c.value for c in SkillCategory]
