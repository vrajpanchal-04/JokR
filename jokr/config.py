"""Load and validate the four YAML config files in config/.

Models are strict (unknown keys rejected) because a typo in a cap must stop
JokR, not silently fall back to a default (C6).
"""

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator

PositiveMoney = Annotated[Decimal, Field(gt=0)]


class ConfigError(Exception):
    """A config file is missing, unreadable, or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Caps(_Strict):
    per_experiment_cad: PositiveMoney
    per_month_cad: PositiveMoney
    llm_per_day_usd: PositiveMoney
    llm_alert_ratio: Annotated[Decimal, Field(gt=0, lt=1)]
    max_live_bets: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def _experiment_fits_in_month(self) -> Self:
        if self.per_experiment_cad > self.per_month_cad:
            raise ValueError("per_experiment_cad cannot exceed per_month_cad")
        return self


class FitRule(_Strict):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    name: Annotated[str, Field(min_length=1)]
    description: Annotated[str, Field(min_length=1)]


class FitRules(_Strict):
    rules: Annotated[list[FitRule], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [r.id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate fit rule ids: {dupes}")
        return self


Weight = Annotated[Decimal, Field(ge=0, le=1)]


class Weights(_Strict):
    pain_intensity: Weight
    demand_signal: Weight
    willingness_to_pay: Weight
    build_cost_inverse: Weight
    distribution_ease: Weight
    competition_gap: Weight
    research_edge: Weight

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        # Decimal keeps 0.2 + 0.15 + ... exact, so equality is safe here.
        total = sum(self.model_dump().values(), Decimal(0))
        if total != Decimal(1):
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class Scoring(_Strict):
    weights: Weights
    g1_threshold: Annotated[Decimal, Field(ge=0, le=10)]
    needs_evidence_min_weight: Weight


class Source(_Strict):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    type: Literal["api", "inbox"]
    enabled: bool
    tos_url: HttpUrl | None = None
    rate_limit_per_min: Annotated[int, Field(gt=0)] | None = None
    path: str | None = None

    @model_validator(mode="after")
    def _fields_for_type(self) -> Self:
        # C3: an API source must name the terms it is read under and a rate limit.
        if self.type == "api" and (self.tos_url is None or self.rate_limit_per_min is None):
            raise ValueError(f"api source {self.name!r} needs tos_url and rate_limit_per_min")
        if self.type == "inbox" and not self.path:
            raise ValueError(f"inbox source {self.name!r} needs path")
        return self


class Sources(_Strict):
    sources: list[Source]

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [s.name for s in self.sources]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate source names: {dupes}")
        return self


class JokrConfig(_Strict):
    caps: Caps
    fit_rules: FitRules
    scoring: Scoring
    sources: Sources


def _load[M: BaseModel](config_dir: Path, filename: str, model: type[M]) -> M:
    path = config_dir / filename
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid: {exc}") from exc


def load_config(config_dir: Path) -> JokrConfig:
    """Load all four files; raise ConfigError naming the first bad file."""
    return JokrConfig(
        caps=_load(config_dir, "caps.yaml", Caps),
        fit_rules=_load(config_dir, "fit_rules.yaml", FitRules),
        scoring=_load(config_dir, "scoring.yaml", Scoring),
        sources=_load(config_dir, "sources.yaml", Sources),
    )
