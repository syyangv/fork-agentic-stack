from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


FORMAL_DIR_ALLOWLIST = (
    "10-Projects",
    "20-Research",
    "30-Decisions",
    "40-Lessons",
)

ALL_DEFAULT_DIRS = (
    "00-Inbox",
    "10-Projects",
    "20-Research",
    "30-Decisions",
    "40-Lessons",
    "90-Archive",
)

# Operational rules policy constant
OPERATIONAL_RULES_POLICY = (
    "操作规则只链接、不镜像：知识库记录架构原因、背景决策与跨任务复用经验，"
    "并显式链接到 .agent/protocols/ 或 skills/ 中的权威执行规则，不复制独立可修改的规则副本。"
)

DEFAULT_WORK_VAULT_PATH = Path("~/obsidian/agent-knowledge").expanduser()
DEFAULT_PERSONAL_VAULT_PATH = Path("~/obsidian/syang").expanduser()
DEFAULT_RUNTIME_DIR = Path("~/.agent/knowledge").expanduser()


@dataclass
class VaultRegistration:
    vault_id: str
    path: Path
    display_name: str = ""
    is_personal: bool = False
    indexed: bool = True
    allowed_dirs: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.path = Path(self.path)
        if not self.display_name:
            self.display_name = self.vault_id


@dataclass
class KnowledgeConfig:
    work_vault: VaultRegistration
    personal_vault: VaultRegistration
    runtime_dir: Path
    protocol_version: str = "1.0.0"

    @classmethod
    def create(
        cls,
        work_vault_path: Optional[Path | str] = None,
        personal_vault_path: Optional[Path | str] = None,
        runtime_dir: Optional[Path | str] = None,
    ) -> KnowledgeConfig:
        wv_path = Path(work_vault_path) if work_vault_path else DEFAULT_WORK_VAULT_PATH
        pv_path = Path(personal_vault_path) if personal_vault_path else DEFAULT_PERSONAL_VAULT_PATH
        rt_path = Path(runtime_dir) if runtime_dir else DEFAULT_RUNTIME_DIR

        return cls(
            work_vault=VaultRegistration(
                vault_id="work",
                path=wv_path,
                display_name="Agent Knowledge Vault",
                is_personal=False,
                indexed=True,
                allowed_dirs=list(FORMAL_DIR_ALLOWLIST),
            ),
            personal_vault=VaultRegistration(
                vault_id="personal",
                path=pv_path,
                display_name="Personal Vault",
                is_personal=True,
                indexed=False,  # Personal vault is NEVER indexed by default
                allowed_dirs=[],  # Unbounded default access is prohibited; requires explicit task grant
            ),
            runtime_dir=rt_path,
        )
