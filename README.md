# Qwen3.8 Flash Next @ Mac (MLX Serve)

在 MacBook Pro (M3 Max / 128GB) 上用 MLX Serve 把 Qwen3.8 Flash Next 跑成
ZCode 日常主力 provider 的完整记录:环境搭建、512K 上下文调优、
双引擎速度对比、准确率抽样验收、进程重启后的前缀缓存实测。

## 一句话结论

**MLX Serve (mixed 4/8bit) 全面胜出 LM Studio (Q3 GGUF)**:
decode 快 3~5 倍(41 vs 13 tok/s @75k)、长上下文 prefill 2 倍快且不衰减、
常驻内存省 8.5GB。配合 YaRN 外推到 512K 上下文 + 28GB 内存/100GB 磁盘
前缀缓存,300k 前缀重发 TTFT 从 857s 降到 5.7s,日常长会话每轮只算增量。

## 仓库内容

| 文件 | 内容 |
|---|---|
| [HANDBOOK.md](HANDBOOK.md) | 主手册:从零复现全流程、512K YaRN 调优、ZCode 接入、运维命令、坑与回滚 |
| [REPORT.md](REPORT.md) | 速度对比报告(原始数据 + 口径说明)与准确率抽样记录 |
| [bench.py](bench.py) | 基准脚本:冷/热 prefill、TTFT、decode 吞吐(`python3 bench.py mlx\|lmstudio`) |
| `result_*.json` | 2026-09-13 各轮实测原始数据 |

## 关键数字(2026-09-13 实测)

| 指标 | LM Studio | MLX Serve |
|---|---|---|
| Decode 吞吐 @75k ctx | 13 tok/s | **41 tok/s** |
| 冷 prefill @75k | 418 s | **172 s** |
| 300k 前缀重发 TTFT | — | **5.7 s**(调优前 857s) |
| 上下文 | 200k | **512k**(YaRN factor 2.0) |
| 常驻内存 | 83.8 GB | **75.3 GB** |

准确率抽样 18/18 通过(代码/数学/指令/512k 深针检索/抗幻觉),
量化 + kv8 + YaRN512K 组合未见质量劣化。

## 环境

- 模型:`ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit`(HuggingFace,磁盘 100GB)
- 引擎:MLX Serve v26.9.2(MLX Core.app),端口 11234
- 详细参数与理由见 HANDBOOK §2-§3
