"""Atenea: a lightweight agent SDK for local models."""
from .agent import Agent, Stats
from .llm import LLM, ModelProfile, set_max_parallel
from .registry import Tool, tool
from .skills import SkillSet

__all__ = ["Agent", "Stats", "LLM", "ModelProfile", "set_max_parallel", "Tool", "tool", "SkillSet"]
__version__ = "0.3.0"
