"""Templates and text definitions for Agent Knowledge Vault."""

from __future__ import annotations

from .config import OPERATIONAL_RULES_POLICY

HOME_TEMPLATE = f"""# Agent Knowledge Vault

欢迎使用 Agent Knowledge Vault。这是独立的 AI Agent 确认工作知识库。

## 核心原则

1. **每类信息只有一个权威来源**：
   - 权威执行规则、Skills、Protocols 归项目和 `.agent` 维护。
   - 本 Vault 负责记录工作背景、架构决策、调研结论与复盘教训。
2. **{OPERATIONAL_RULES_POLICY}**
3. **草稿自动生成，正式知识人工审批**：
   - 只有经过确认的笔记才进入正式目录并参与检索。
   - 草稿暂存在 `00-Inbox/`，默认不进入检索。

## 目录结构

- `00-Inbox/` — 自动生成的待审草稿（默认不参与检索）
- `10-Projects/` — 核心项目背景与架构上下文（正式检索目录）
- `20-Research/` — 调研报告、技术对比与事实核实（正式检索目录）
- `30-Decisions/` — 架构决策记录 ADR（正式检索目录）
- `40-Lessons/` — 经确认的教训与经验沉淀（正式检索目录）
- `90-Archive/` — 历史归档材料（默认不参与检索）
"""

INBOX_README_TEMPLATE = """# 00-Inbox 待审草稿箱

本目录用于存放任务执行过程中由 Agent 自动提议的知识草稿 (`KnowledgeDraft`)。

## 注意事项

1. 本目录下的草稿**默认不进入知识检索**，以防止未核实的推论污染上下文。
2. 必须经过人工审阅与确认 (`review_apply`) 后，草稿才会提升至正式知识目录 (`10-Projects`, `20-Research`, `30-Decisions`, `40-Lessons`)。
3. 审阅拒绝的草稿将被标记归档，不会反复推送到待审队列。
"""

PROJECT_TEMPLATE = """---
id: {{id}}
title: {{title}}
type: project
status: active
created: {{created}}
updated: {{updated}}
rules_ref:
  - ref: .agent/protocols/permissions.md
    note: "操作规则只链接、不镜像"
---

# {{title}}

## 1. 项目背景与目标 (Context & Goals)

## 2. 核心架构与决策 (Architecture & Decisions)

## 3. 运行状态与约束 (Active State & Constraints)

## 4. 关联规则与出处 (References & Rules)
<!-- 请链接到 .agent/protocols/ 或 skills/ 规则路径，不复制规则正文 -->
"""

RESEARCH_TEMPLATE = """---
id: {{id}}
title: {{title}}
type: research
status: completed
created: {{created}}
sources: []
rules_ref: []
---

# {{title}}

## 1. 问题与假设 (Question & Hypothesis)

## 2. 调研发现 (Findings & Analysis)

## 3. 证据与验证 (Evidence & Verification)

## 4. 权衡与后续 (Tradeoffs & Follow-ups)
"""

DECISION_TEMPLATE = """---
id: {{id}}
title: {{title}}
type: decision
status: accepted
date: {{date}}
rules_ref: []
---

# {{title}}

## 1. 背景与诉求 (Context)

## 2. 决策内容 (Decision)

## 3. 影响与权衡 (Consequences & Tradeoffs)

## 4. 关联规则与实现 (Related Rules & Implementation)
<!-- 操作规则只链接、不镜像 -->
"""

LESSON_TEMPLATE = """---
id: {{id}}
title: {{title}}
type: lesson
status: accepted
date: {{date}}
trigger: ""
rules_ref: []
---

# {{title}}

## 1. 现象与触发 (What Happened)

## 2. 根因与模式分析 (Root Cause & Pattern)

## 3. 预防准则 (Prevention & Guidance)
<!-- 操作规则只链接、不镜像：如影响执行协议，请在对应 skill/protocol 处维护规则并在此附链接 -->

## 4. 证据依据 (Evidence)
"""

DEFAULT_TEMPLATES = {
    "project.md": PROJECT_TEMPLATE,
    "research.md": RESEARCH_TEMPLATE,
    "decision.md": DECISION_TEMPLATE,
    "lesson.md": LESSON_TEMPLATE,
}
