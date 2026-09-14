# eval/ — ZCode + 本地模型 agentic 能力评测

回答的问题:MLX Serve 上的 Qwen3.8 Flash Next(mixed 4/8bit + YaRN512K + kv8)
配合 ZCode,多步 coding agent 能力有没有被部署"劣质化"?
速度结论在 ../REPORT.md,这里只测 agentic 能力。

## 方法(一句话)

SWE-bench 风格:python-dotenv / python-semver / humanize 三个真实开源库各取 2 个
真实 bugfix,回滚到 fix 前、只打测试补丁当验收规格;ZCode 干净子代理(同模型)
按用户口吻的 bug 报告修源码,评测方独立复跑测试 + 与官方 diff 逐行对照判分。

## 结果

**6 题 = 5 PASS + 1 PARTIAL,未见劣质化信号。**
细节、逐题数据、失分点分析(semver-2 验证方法论误报)与结论边界,见 [RESULTS.md](RESULTS.md);
逐题判分原始记录见 [run_notes.md](run_notes.md)。

## 目录内容

| 路径 | 说明 |
|---|---|
| [RESULTS.md](RESULTS.md) | 评测报告(结论 + 逐题数据 + 保留意见) |
| [run_notes.md](run_notes.md) | 判分时的逐题原始记录 |
| [setup_tasks.sh](setup_tasks.sh) | 任务包构建脚本(可复现) |
| `.cache/repos/` | 带完整历史的源仓库(**含答案,做题时须对子代理隔离**) |
| `tasks/<题名>/` | 每题的已修复工作树(`git diff` 可见改动;含 venv) |

`tasks/` 与 `.cache/` 被本目录 `.gitignore` 排除,不入库;重建用
`bash eval/setup_tasks.sh`。

## 复现注意

- 任务书必须改写成用户口吻,**禁止把 fix 的 commit message 原文给子代理**(泄露根因)。
- 判分不能只信子代理自报"无回归":评测方要独立跑全量并与构建时基线对比
  (semver-2 的失分正是子代理把新回归误归为存量)。
- 子代理必须用全新干净上下文,且任务目录内 `git log` 已 gc 掉 fix 提交,防止翻历史作弊。
