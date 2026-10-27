// 《小八记得》：一个每次醒来都不记得昨天的经理，和它留下的档案。
// 留进档案的每一句都摘自原文：Playbook 打法/偏好/踩坑、历史版本记录、私人助理 AGENTS.md、用户原话（2026-10-14 读取）。
// 会话里滚动的工作文字是示意，由真实的命令名、路径和流程拼成，不是某次会话的原始记录。

export const FPS = 30;
export const DURATION = 2130;

export const C = {
  bg: "#0D0D0F",
  text: "#E9E5DC",
  dim: "#6A665F",
  faint: "#2C2A27",
  amber: "#F2A541",
};

export type Session = {
  from: number;
  to: number;
  agent: string;
  date: string;
  lines: string[];
  keep: string; // 被留下来的那一句（原文）
  keepAt: number; // 它在 lines 之后插入的位置
  tag: string; // 在档案里显示的日期标签
};

export const SESSIONS: Session[] = [
  {
    from: 240, to: 480, agent: "Codex", date: "2026-08-22", tag: "08.22",
    lines: [
      "整理 音效素材库 → 指纹扫描",
      "生成 音效指纹库_v3.csv",
      "SFX 匹配：whoosh ~1.2s",
      "交付 QC：音频统一 48 kHz",
      "视觉知识：先拆解，再沉淀",
      "提示词分工：CINEDANCE / Lira / ACTING",
      "项目结束，蒸馏经验",
    ],
    keep: "项目结束后的长期资产是规则、索引和 QC 经验，不是每个版本的媒体文件。",
    keepAt: 7,
  },
  {
    from: 480, to: 660, agent: "gpt_codex · claude_code", date: "2026-09-04", tag: "09.04",
    lines: ["init → SQLite 任务队列", "租约恢复 · 指数退避", "两个 Agent 并行执行", "裁判：确定性评分", "事件链：只追加 · 哈希"],
    keep: "至少两个成功才允许裁判交付",
    keepAt: 5,
  },
  {
    from: 660, to: 810, agent: "gpt_codex · claude_code", date: "2026-09-07", tag: "09.07",
    lines: ["统一会话入口路由", "回答来源证明", "两阶段用户裁决", "tests: 104 passed"],
    keep: "用户仍是唯一最终裁决者",
    keepAt: 3,
  },
  {
    from: 810, to: 930, agent: "agy_review · dsh_review", date: "2026-10-05", tag: "10.05",
    lines: ["build_release.py --acceptance", "AGY 审查：PASS", "DSH 审查：PASS"],
    keep: "未明确要求不得自主备份，可以提醒",
    keepAt: 3,
  },
  {
    from: 930, to: 1110, agent: "Claude Code", date: "2026-10-08", tag: "10.08",
    lines: ["读取 MEMORY.md", "用户：H3 视频技能，第二大脑里有记录", "用户：你没习惯看第二大脑"],
    keep: "经理可以换，但是这个企业不能倒。",
    keepAt: 3,
  },
  {
    from: 1110, to: 1215, agent: "某个 Agent", date: "2026-10-09", tag: "10.09",
    lines: ["执行前先检查前提", "区分事实、预测和观点"],
    keep: "不要一味迎合，要独立判断。",
    keepAt: 2,
  },
  {
    from: 1215, to: 1320, agent: "Claude Code", date: "2026-10-10", tag: "10.10",
    lines: ["v1.5.1 已上线", "用户：审美手册没进这一版"],
    keep: "用户明确指出问题时，当场记录，不等“交付”，也不等用户提醒",
    keepAt: 2,
  },
  {
    from: 1440, to: 1590, agent: "Claude Code", date: "2026-10-14", tag: "10.14",
    lines: ["migrate_env.py apply", "8 项搬进 配置/", "verify：4 项不满足", "从旧路径启动，本来就报旧路径", "不回滚"],
    keep: "环境迁移 verify 把“从旧路径调用就报旧路径”判成失败",
    keepAt: 5,
  },
];

// 10.10 — 10.13 之间快速落进档案的踩坑标题（原文）
export const FLURRY = {
  from: 1326,
  every: 9,
  titles: [
    "旧路径和旧端口引用",
    "音频采样率不一致",
    "后台进程用 nohup 启动后自行消失",
    "版本号到两位数后子串比较误判",
    "同一份审查材料，两轮结论不同",
    "审查材料里写死了会变的数字",
    "写进第二大脑不等于别的 Agent 找得到",
    "用户指出了问题，却没有当场记下来",
    "提示词写了“不要备份”，发布代码仍无条件备份",
    "把审美手册做成了考用户的测验",
    "没查资料就给“AI 视频模型做不好转场”下了结论",
    "DeepSeek Harness 个别请求带 384000 的输出上限，tierflow 直接 400",
  ],
};

export const COUNTS = "第二大脑 · 34 条经验　　踩坑 · 34 条　　偏好 · 31 条";

export const OPENING = { line1: "我醒来的时候，不记得昨天。", line2: "这里还什么都没有。" };

// 转折：别的 Agent 醒来，读同一份档案
export const TURN = {
  from: 1590,
  wake: 1600,
  expand: [1670, 1750] as [number, number],
  agents: ["DeepSeek Harness", "Antigravity", "Codex", "Claude Code"],
  agentsFrom: 1760,
  agentEvery: 30,
  to: 1890,
};

export const ENDING = { from: 1890, l1: "我还是不记得昨天。", l1At: 1915, l2: "但小八记得。", l2At: 2005, titleAt: 2060 };
