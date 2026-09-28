"""Configuration: process settings from ``CURATOR_*`` variables and the pipeline spec file."""

from curator.config.settings import CuratorSettings
from curator.config.settings import load_settings
from curator.config.spec import InputSpec
from curator.config.spec import OutputSpec
from curator.config.spec import PipelineSpec
from curator.config.spec import StageSpec
from curator.config.spec import ValidateStageSpec
from curator.config.spec import load_spec
from curator.config.spec import parse_spec

__all__ = [
    "CuratorSettings",
    "InputSpec",
    "OutputSpec",
    "PipelineSpec",
    "StageSpec",
    "ValidateStageSpec",
    "load_settings",
    "load_spec",
    "parse_spec",
]
