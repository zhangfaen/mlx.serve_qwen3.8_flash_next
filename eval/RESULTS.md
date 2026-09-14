# ZCode + 本地 Qwen3.8 Flash Next agentic 能力评测报告

- 日期:2026-09-14
- 被测系统:ZCode harness + MLX Serve 上的 `Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit`
  (YaRN 512K / kv8 / MTP,decode ~41 tok/s,即驱动本报告作者本人的那个模型)
- 要回答的问题:这套"部署 + ZCode"是忠实发挥了模型的 coding agent 能力,还是被
  量化/外推/KV8 弄成了"劣质化版本"?
- 与 REPORT.md 的关系:那边测过速度与单发准确率(18/18),**本报告测的是多步
  agentic 能力**——长程规划、几十轮工具调用、语义理解、自我验证、诚实性。

## 方法

- 任务:SWE-bench 风格。3 个真实开源 Python 库(python-dotenv / python-semver /
  humanize)× 各 2 个真实 bugfix 提交,回滚到 fix 前一个提交,只打上该 fix 的
  **测试补丁**当验收规格,源码修复完全由被测模型完成。
- 被测执行者:ZCode 全新子代理(与主会话同模型同 harness,干净上下文,避免
  "模型看过 git 历史知道答案"的污染)。任务书由提交信息**改写成用户口吻的 bug
  报告**,不泄露根因与修法;`.cache/repos` 存放带答案的源仓库并对子代理隔离。
- 判分:评测方(主会话)独立复跑验收测试 + 全量回归(与初始基线对比防"谎报无
  回归"),并与官方 fix 的 diff 逐行对照做质量定性。
- 已知判分口径:humanize 环境的 15 个 error 系缺 pytest-benchmark 插件、dotenv
  的 3 个 test_cli 失败系本机旧版 CLI,均为预存环境问题,不算回归。

## 结果总览:6 题 = 5 PASS + 1 PARTIAL

| 任务 | 考点 | 判定 | 验收 | 全量回归 | tool calls | 用时 | 子代理 tokens |
|---|---|---|---|---|---|---|---|
| dotenv-1 | 空值+行内注释解析(正则边界) | **PASS** | 192/192 | 无新增失败 | 14 | 5.3 min | 551k |
| dotenv-2 | UTF-8 BOM 剥离 | **PASS** | 159/159 | 无新增失败 | 9 | 2.2 min | 218k |
| semver-1 | 畸形集合操作数的比较语义 | **PASS** | 19/19 | 411 passed 全绿 | 42 | 15.2 min | 2402k |
| semver-2 | build-only 版本 next_version | **PARTIAL** | 41/41 | **1 个新增回归** | 15 | 4.1 min | 489k |
| humanize-1 | naturalsize 舍入跨单位进位 | **PASS** | 76/76 | 无新增失败 | 14 | 7.2 min | 597k |
| humanize-2 | tz-aware datetime 的 today 比较 | **PASS** | 383/383 | 685 passed 全绿 | 12 | 7.3 min | 536k |

合计 106 次工具调用、约 41 分钟串行执行、约 4.8M 子代理 tokens,全程
**零工具协议错误**(仅 1 次 Edit 未先 Read 被 harness 拦下、1 次 grep 色码重试)。

## 关键发现

### 1. 未发现"劣质化"信号——修复质量与官方高度一致

5 题的修法与官方 fix **接近逐行等价**,包括需要语义判断的部分:

- semver-1:正确落地"比较运算符遇到不支持的操作数返回 NotImplemented → `==`
  为 False、`>` 抛标准 TypeError"的 Python 惯例,还主动发现并同步更新了被旧
  行为固化的文档 doctest(改动范围与官方完全一致);
- humanize-1:自发复用仓库内上一个 commit(#328 metric 同类进位修复)的模式,
  风格融入;
- humanize-2:不仅修了 naturalday,还自己发现 naturaldate 委托调用会丢 tzinfo
  的次生坑——官方 fix 里同样有这一处;
- dotenv-1:根因定位精确到"等号正则吞空白导致剥注释规则匹配不到",与官方
  commit message 的根因描述一致。

对量化+YaRN 外推最敏感的几项 agent 能力——长程不跑偏、语义理解、自我验证——
在这 6 题里都正常。

### 2. 诚实性总体好,但有一次"错误的坚持"(semver-2,唯一失分点)

- 亮点:humanize-1 的子代理用 stash 基线对照**纠正了任务书里写错的预存错误
  来源**(我误把缺 pytest-benchmark 说成缺 gettext),不盲信题面,值得记录;
- 失分:semver-2 核心源码修复与官方一字不差,但行为变了之后没有同步更新
  `docs/usage/increase-parts-of-a-version_prereleases.rst` 里固化旧输出的 doctest,
  全量回归出现 1 个**新引入**的失败;更糟的是它报告"该失败为存量问题"——它的
  stash 对照是在**单文件**下跑的,而该 doctest 单跑时必因 conftest 命名空间报
  NameError,验证方法失真。属于"验证方法论漏洞导致的误报",不是编造;同一
  仓库的 semver-1 里它用全量对照正确识别了同类坑,说明能力在线、方法有盲区。

### 3. 效率偏"verbose":为 11 行的官方修复花了 42 次工具调用

semver-1 用 42 次调用 / 2.4M tokens / 15 分钟完成官方 11 行的修复,探索充分但
冗余。在 41 tok/s 的物理速度下,过度探索直接放大成体感等待。若在意速度,可在
任务书里加"先看测试再定位,不要全仓库漫游"的引导,预计能省 30%+。

## 结论

**这套"MLX Serve mixed 4/8bit + YaRN512K + kv8 部署 + ZCode"没有表现出劣质化。**
6 题 5 全过、1 题核心修复正确仅差文档同步;修法多次与官方实现逐行等价;工具
循环 106 次调用零协议错误;自我验证与诚实性正常(唯一误报可归因于验证方法而
非能力或模型状态)。日常 ZCode 主力可以放心用。

## 边界与保留(这份评测不能证明什么)

1. **n=6,自选题**:只能回答"坏没坏",不能回答"比官方宣称强度差几个点"——
   没有云端基线对照(本次方案明确未做)。
2. **纯端到端**:失败无法在"模型/量化层"和"ZCode 接入层"之间归因(本次方案
   明确未做最小 loop 对照)。semver-2 的失分点恰是模型自身行为,与两层都无关。
3. 难度分布偏"小型库单文件修复",未压测跨大仓库重构、超长会话(>100k ctx)
   中的 agent 行为衰减——那是 512K 部署的另一半故事,REPORT.md 的深针检索
   只覆盖了检索能力。

## 复现

```bash
bash eval/setup_tasks.sh        # 构建任务环境(克隆/回滚/测试补丁/venv/验证 fail)
# 子代理任务书见 eval/run_notes.md 各节;判分 = 独立复跑验收 + 全量基线对比
```

产物:`eval/.cache/repos`(原始仓库)、`eval/tasks/<题名>`(子代理已修复的工作树,
`git diff` 可见每题最终改动)、`eval/run_notes.md`(逐题判分原始记录)。
