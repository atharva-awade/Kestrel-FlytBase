"""Rules — declarative temporal detection, triage and narrative."""

from kestrel.rules.compiler import (
    BacktestReport,
    RuleCompiler,
    observations_from_db,
)
from kestrel.rules.dsl import Rule, rule_json_schema
from kestrel.rules.engine import Observation, RuleEngine, RuleResult
from kestrel.rules.pack import default_rules, rule_by_id
from kestrel.rules.triage import NarrativeBuilder, ThreatNarrative, Triage

__all__ = [
    "BacktestReport",
    "NarrativeBuilder",
    "Observation",
    "Rule",
    "RuleCompiler",
    "RuleEngine",
    "RuleResult",
    "ThreatNarrative",
    "Triage",
    "default_rules",
    "observations_from_db",
    "rule_by_id",
    "rule_json_schema",
]
