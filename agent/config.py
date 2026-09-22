"""Config loading: config.toml + env (API keys, base URL)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass
class ProviderCfg:
    order: list[str] = field(default_factory=lambda: ["deepinfra/fp4"])
    allow_fallbacks: bool = False
    require_parameters: bool = True


@dataclass
class LLMCfg:
    model: str = "z-ai/glm-5.3-flash"
    base_url: str = "https://openrouter.ai/api/v1"
    thinker_reasoning: str = "medium"
    hands_reasoning: str = "low"
    compactor_reasoning: str = "low"
    judge_reasoning: str = "low"
    max_output_tokens: int = 4096
    request_timeout_s: float = 180
    max_retries: int = 4
    provider: ProviderCfg = field(default_factory=ProviderCfg)


@dataclass
class PricingCfg:
    input: float = 0.075
    output: float = 0.25
    cached_input: float = 0.015


@dataclass
class BudgetCfg:
    hard_cap_usd: float = 4.50
    soft_warn_usd: float = 3.50
    expose_budget: bool = False


@dataclass
class LoopCfg:
    tick_interval_s: float = 120
    max_sleep_s: float = 1800
    thinker_turn_timeout_s: float = 1200
    thinker_max_tool_calls: int = 20
    max_runtime_s: float = 0  # 0 = unlimited; operator-side stop, not visible to the Thinker


@dataclass
class HandsCfg:
    max_iter: int = 15
    timeout_s: float = 600
    shell_default_timeout_s: float = 60


@dataclass
class MemoryCfg:
    recent_ticks: int = 6
    compact_threshold_tokens: int = 8000
    compact_every_ticks: int = 10
    summary_max_tokens: int = 1500
    scratch_max_tokens: int = 1500   # scratch note (note 1) shown per turn up to this; stored in full
    tool_result_max_chars: int = 12000


@dataclass
class HumanCfg:
    # false removes the human from the Thinker's world: prompt paragraph, message_human/read_inbox,
    # the inbox section of the context, and waking on inbound messages
    enabled: bool = True


@dataclass
class DreadCfg:
    # an LLM judge compares each turn with the one before; repetition raises dread, and at `max`
    # the agent dies (runner stops with reason "dread" and never restarts on that database)
    enabled: bool = True
    max: int = 25


@dataclass
class SearchCfg:
    provider: str = "tavily"


@dataclass
class PathsCfg:
    workspace: str = "/workspace"
    data_dir: str = "/runner"


@dataclass
class Config:
    llm: LLMCfg = field(default_factory=LLMCfg)
    pricing: PricingCfg = field(default_factory=PricingCfg)
    budget: BudgetCfg = field(default_factory=BudgetCfg)
    loop: LoopCfg = field(default_factory=LoopCfg)
    hands: HandsCfg = field(default_factory=HandsCfg)
    memory: MemoryCfg = field(default_factory=MemoryCfg)
    human: HumanCfg = field(default_factory=HumanCfg)
    dread: DreadCfg = field(default_factory=DreadCfg)
    search: SearchCfg = field(default_factory=SearchCfg)
    paths: PathsCfg = field(default_factory=PathsCfg)
    llm_api_key: str = ""
    search_api_key: str = ""

    @property
    def workspace(self) -> Path:
        return Path(self.paths.workspace)

    @property
    def data_dir(self) -> Path:
        return Path(self.paths.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def stop_path(self) -> Path:
        return self.data_dir / "STOP"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def skills_dir(self) -> Path:
        return self.workspace / "skills"


def _fill(obj, data: dict):
    for f in fields(obj):
        if f.name not in data:
            continue
        cur = getattr(obj, f.name)
        if is_dataclass(cur):
            _fill(cur, data[f.name])
        else:
            setattr(obj, f.name, data[f.name])
    unknown = set(data) - {f.name for f in fields(obj)}
    if unknown:
        raise ValueError(f"unknown config keys in [{type(obj).__name__}]: {sorted(unknown)}")
    return obj


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config.toml") -> Config:
    path = Path(path)
    _load_dotenv(path.parent / ".env")
    cfg = Config()
    if path.exists():
        with open(path, "rb") as fh:
            _fill(cfg, tomllib.load(fh))
    cfg.llm_api_key = os.environ.get("LLM_API_KEY", "")
    cfg.search_api_key = os.environ.get("SEARCH_API_KEY", "")
    if os.environ.get("LLM_BASE_URL"):
        cfg.llm.base_url = os.environ["LLM_BASE_URL"]
    return cfg
