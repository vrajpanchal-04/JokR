"""Load and validate the four YAML config files in config/.

Models are strict (unknown keys rejected) because a typo in a cap must stop
JokR, not silently fall back to a default (C6).
"""

from decimal import Decimal
from pathlib import Path, PurePosixPath
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


# A plain, lowercase, public FQDN: no scheme, port, userinfo, wildcard, trailing
# dot or IP literal (the TLD must be alphabetic). Exact match is the only rule
# GuardedClient applies, so anything looser here would widen C3.
_HOST = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
Host = Annotated[str, Field(pattern=_HOST, max_length=253)]
Query = Annotated[str, Field(min_length=1, max_length=200)]


class HackerNewsParams(_Strict):
    kind: Literal["hackernews"]
    queries: Annotated[tuple[Query, ...], Field(min_length=1)]
    tags: Annotated[
        tuple[Literal["story", "ask_hn", "show_hn", "comment"], ...], Field(min_length=1)
    ] = ("story", "ask_hn", "comment")
    # Fetched whole, with no query: every recent Ask HN ("is there a tool for...")
    # and Show HN post is a candidate pain or competitor signal.
    browse_tags: tuple[Literal["ask_hn", "show_hn"], ...] = ("ask_hn", "show_hn")
    hits_per_page: Annotated[int, Field(ge=1, le=1000)] = 100
    lookback_days: Annotated[int, Field(ge=1, le=365)] = 30


class RedditParams(_Strict):
    kind: Literal["reddit"]
    subreddits: Annotated[
        tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{2,21}$")], ...], Field(min_length=1)
    ]
    listing: Literal["new", "top", "hot"] = "new"
    limit: Annotated[int, Field(ge=1, le=100)] = 100
    lookback_days: Annotated[int, Field(ge=1, le=365)] = 7
    max_pages_per_subreddit: Annotated[int, Field(ge=1, le=10)] = 3


class ArxivParams(_Strict):
    kind: Literal["arxiv"]
    categories: Annotated[
        tuple[Annotated[str, Field(pattern=r"^[a-z-]+(\.[A-Za-z]{2})?$")], ...], Field(min_length=1)
    ]
    page_size: Annotated[int, Field(ge=1, le=2000)] = 100
    lookback_days: Annotated[int, Field(ge=1, le=365)] = 14


class InboxParams(_Strict):
    kind: Literal["inbox"]
    max_file_bytes: Annotated[int, Field(gt=0, le=50_000_000)] = 5_000_000
    max_rows_per_file: Annotated[int, Field(gt=0, le=100_000)] = 10_000
    max_files: Annotated[int, Field(gt=0, le=1_000)] = 100
    max_depth: Annotated[int, Field(ge=0, le=5)] = 2


SourceParams = Annotated[
    HackerNewsParams | RedditParams | ArxivParams | InboxParams, Field(discriminator="kind")
]


class Source(_Strict):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
    type: Literal["api", "inbox"]
    enabled: bool
    params: SourceParams
    tos_url: HttpUrl | None = None
    allowed_hosts: tuple[Host, ...] = ()
    min_interval_s: Annotated[float, Field(ge=0, le=3600)] | None = None
    max_requests: Annotated[int, Field(gt=0, le=10_000)] | None = None
    # Wall-clock limit for one source in one run, so a slow site can't stall Scout.
    max_runtime_s: Annotated[int, Field(ge=1, le=3600)] = 300
    path: str | None = None

    @model_validator(mode="after")
    def _fields_for_type(self) -> Self:
        # One connector per source: the params kind picks the connector, so it
        # must match the name or a source could borrow another's behaviour.
        if self.params.kind != self.name:
            raise ValueError(f"params.kind {self.params.kind!r} must equal source name")
        if self.type == "api":
            # C3: an API source must name its terms, its hosts and its pacing.
            missing = [
                f for f in ("tos_url", "min_interval_s", "max_requests") if getattr(self, f) is None
            ]
            if not self.allowed_hosts:
                missing.append("allowed_hosts")
            if missing:
                raise ValueError(f"api source {self.name!r} needs {', '.join(missing)}")
        else:
            if self.allowed_hosts or self.tos_url is not None:
                raise ValueError(f"inbox source {self.name!r} cannot have hosts or tos_url")
            if not self.path or not _inside_data_dir(self.path):
                raise ValueError(f"inbox source {self.name!r} needs a path under data/")
        return self


def _inside_data_dir(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        not PurePosixPath(path).is_absolute()
        and ".." not in parts
        and len(parts) >= 2
        and parts[0] == "data"
    )


class Sources(_Strict):
    sources: list[Source]

    def get(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [s.name for s in self.sources]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate source names: {dupes}")
        return self


IntentTerm = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9 +#./'-]*$")
]


class IntentGroup(_Strict):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=32)]
    # Weights never go below 1: the score is a boost for intent, never a penalty.
    weight: Annotated[float, Field(ge=1, le=10)]
    terms: Annotated[tuple[IntentTerm, ...], Field(min_length=1)]


class IntentLexicon(_Strict):
    """config/intent.yaml: the commercial-intent lexicon (§3b). Bump `version` on any edit."""

    version: Annotated[str, Field(min_length=1, max_length=32)]
    cap: Annotated[float, Field(ge=1, le=999)]
    groups: Annotated[tuple[IntentGroup, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_names_and_terms(self) -> Self:
        names = [g.name for g in self.groups]
        if len(names) != len(set(names)):
            raise ValueError("duplicate intent group names")
        terms = [t for g in self.groups for t in g.terms]
        if len(terms) != len(set(terms)):
            raise ValueError("an intent term appears more than once")
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


def load_intent(config_dir: Path) -> IntentLexicon:
    """Scout's lexicon. Separate from load_config because only Scout needs it."""
    return _load(config_dir, "intent.yaml", IntentLexicon)
