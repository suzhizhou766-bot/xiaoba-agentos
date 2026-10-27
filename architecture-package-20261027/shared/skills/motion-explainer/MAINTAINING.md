# 维护说明

- **源头**：本仓库。改完后，把整个文件夹同步到你用的 agent 的 skills 目录（例如 `~/.claude/skills/`、`~/.codex/skills/`）。
- **加规则要谨慎**：只加「别犯什么错」的原则和下限，不加像素级配方；新规则加进去前先跑一次盲测，确认片子变好了再保留（v0.2 的教训：单支片子的评审意见会过拟合）。
- **改引擎**：`assets/template/engine/` 改完，用 `assets/template/demo/` 跑一遍 `score.py` → `engine/run.sh` → `checks/check_all.sh` 验证。
- **验证 skill 好不好用**：同一个需求，分别让另一个模型用 skill、Opus 裸跑各做一版，盲测排名（对比脚本没有随本仓库公开）。
