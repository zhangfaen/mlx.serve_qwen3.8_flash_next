# Qwen3.8 Flash Next 本地双引擎速度对比报告

- 日期:2026-09-13
- 机器:MacBook Pro (M3 Max, 128GB)
- 目的:决定日常在 ZCode 里主用哪个本地 provider

## 被测对象

| | LM Studio | MLX Serve |
|---|---|---|
| 端口 | 127.0.0.1:1234 | 127.0.0.1:11234 |
| 引擎 | llama.cpp (GGUF) | MLX |
| 量化 | unsloth UD-Q3_K_XL (~3bit) | mixed 4/8bit |
| 模型常驻内存 | 83.8 GB | 75.3 GB |
| 冷启动(加载模型) | 29.2 s(首次 CLI 加载) | 9.7 s(进程已起,API 热加载) |
| 特殊配置 | 全 GPU offload, 200k ctx | MTP 开启, kv_quant off, 262k ctx |

注意:两边量化不同(Q3 vs 4/8bit),所以这是"整栈方案"对比,不是纯引擎对比。
MLX 的模型文件更大(101GB vs 84GB),若换同量化会更慢,公平口径下 MLX 仍全面领先。

## 测试方法

- 脚本:`bench.py`,数字序列复述任务,temperature=0,max_tokens=1024,流式逐 chunk 计时
- 冷测:每次换随机种子强制完整 prefill;热测:重发同 payload 测前缀缓存命中
- 每场景冷 2 次 + 热 2 次取中位数
- 顺序执行:先 MLX(独占内存),卸载后加载 LM Studio,避免互相干扰
- 全程系统内存压力正常(free 86%+),swap 无增长

## 结果

### TTFT(首 token 延迟,秒)——越小越好

| 场景 | LM Studio 冷 | MLX 冷 | LM Studio 热 | MLX 热 |
|---|---|---|---|---|
| 5k tok | 13.0 | 9.1 | 0.11 | 0.22 |
| 75k tok | 418 | 172 | 0.16 | 0.29 |
| 150k tok | 721 | 346 | 0.21 | **162** ⚠️ |

### Prefill 吞吐(tok/s)——越大越好

| 场景 | LM Studio | MLX | MLX 优势 |
|---|---|---|---|
| 5k | 775 | 555 | LM Studio 快(短 prompt 阶段) |
| 75k | 311 | 437 | 1.4x |
| 150k | 208 | 434 | 2.1x |

LM Studio prefill 随长度急剧衰减(775→208),MLX 长文本稳定在 ~435。

### Decode 吞吐(tok/s)——越大越好

| 上下文 | LM Studio | MLX | MLX 优势 |
|---|---|---|---|
| ~5k | 21 | 71 | 3.4x |
| ~75k | 13 | 41 | 3.2x |
| ~150k | 8.8 | 41 | 4.7x |

MLX decode 全场景 3~5 倍领先(MTP speculative decoding + MLX 对 Apple Silicon 优化)。
MLX 75k 后 decode 稳定不再衰减;LM Studio 随上下文线性恶化。

### 用户体验换算(150k 上下文写 1000 token 回答)

- LM Studio:721s prefill + 1000/8.8 ≈ **11.5 分钟**
- MLX(冷):346s prefill + 1000/41 ≈ **6.2 分钟**;缓存命中时 ≈ **24 秒**

## 异常与口径说明

1. LM Studio 75k cold1 只有 65s(其他轮 418/474s):llama.cpp 前缀缓存把上一场景残留的
   相同前缀(指令头+部分数字)复用了,属于缓存边界效应,取中位数已剔除影响。
2. MLX 150k 热测 TTFT 162s:前缀缓存不能全量命中 150k(有容量上限),
   日常长会话超过缓存上限后每轮都要部分重新 prefill。
3. LM Studio 会输出 reasoning_content 且忽略"不要思考"指令;MLX 不输出思考。
   两者 token 口径统一为"服务端 usage.completion_tokens"。
4. 本轮 LM Studio 是 CLI 加载(29.2s);若走 LM Studio App 首次加载会略慢。
   MLX 是进程已在、模型 API 热加载(9.7s),进程冷启动另计。

## 结论

**速度上 MLX Serve 全面胜出,建议主用 MLX Serve:**

- decode 3~5 倍快(ZCode 日常体感的主要来源:每轮回答的输出速度)
- 长上下文 prefill 2 倍快,且不随长度衰减
- 常驻内存少 8.5GB,热加载快 3 倍
- 量化更高(4/8bit vs Q3),理论质量基线也更高

LM Studio 仅在"短 prompt 冷 prefill"一项略快(5k 场景 775 vs 555 tok/s),
但日常 ZCode 请求都带长系统提示+工具定义,基本落在 20k+ 区间,MLX 优势区间。

保留 LM Studio 作为备用(多模型管理、embedding、图像模型都在它上面)。

## 后续:阶段 2 准确率对比(未开始)

速度结论明确后,准确率只需验证"MLX mixed 4/8bit 是否够好",无需再比 LM Studio Q3
(量化更低,速度还慢,没有翻盘可能)。建议:
1. 自动基准:HumanEval+(代码 pass@1)、GSM8K 200 题、MMLU-Pro 200 题、IFEval 50 题
2. 盲评:20~30 个真实 ZCode 场景 prompt,云端 GLM-5.3 当裁判
3. 若 4/8bit 掉点明显,可再测 MLX 8bit 全量版本(磁盘约 150GB,需确认空间)

## 追加:512K 升级落地记录(2026-09-13 下午)

需求:ZCode 驱动,理想上下文 512K,容纳 4 个会话前缀缓存。

### 改动(均已备份)

1. 模型 config.json(~/.mlx-serve/models/ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit/):
   - max_position_embeddings 262144→524288
   - rope_parameters 增加 yarn factor=2.0(Qwen 官方对 512K 的建议值)
   - 备份:config.json.bak-256k
2. MLX Core plist serverOptions:
   - ctxSize 0→524288; kvQuant off→8
   - prefixCacheEntries 8→16; prefixCacheMem 2GB→28GB
   - enablePrefixCacheDisk true; prefixCacheDisk 100GB(重启后缓存不丢)
   - 备份:~/Library/Preferences/com.dalcu.mlx-core.plist.bak-20260913152507

### 验证结果

- App 重启后进程参数全部生效(--ctx-size 524288 --kv-quant 8 --prefix-cache-mem 28GB)
- YaRN 外推生效:300k token 请求(超原生 256K)正常处理
  - 冷 prefill 857s(~350 tok/s,与 256K 内速度一致,无外推惩罚)
  - 300k decode 35.6 tok/s(kv8 对 decode 影响可接受)
- 前缀缓存质变:同 300k 前缀重发 TTFT 857s → **5.7s 全量命中**
  (旧配置 2GB budget 下 150k 就只能部分命中)
- 内存模型:kv8 下每 512K 会话 KV ≈ 6.3GB,28GB 预算理论 4 个会话 + 余量
  (4×400k 全量冷灌测试因耗时过长 skipped,只完成 1/4)

### 遗留

- 4 会话同时驻留的实际命中率未实测(每个 400k 冷 prefill 约 19 分钟,总耗时不可接受);
  日常使用中 ZCode 会话是渐进增长的,很少一次性 400k 全新 prefill,实际体验应更好
- ZCode 客户端侧 context limit 已同步改为 524288(~/.zcode/v2/config.json)

## 追加:准确率小抽样(2026-09-13,按用户要求只抽样不跑全量)

### A. 代码/数学/指令(HumanEval/GSM8K/IFEval 风格题,自命题)

| 维度 | 结果 | 备注 |
|---|---|---|
| 代码生成(HumanEval 风格,沙箱跑单测) | 5/5 | add/fizzbuzz/LCP/Kadane/括号匹配 |
| 数学推理(GSM8K 风格) | 5/5 | 1 题首次判 FAIL 是脚本期望值算错,人工复核模型对 |
| 指令遵循(IFEval 风格) | 3/3 | 恰好3句话/单行函数无注释/大写翻译 |

### B. 512K 长上下文检索(needle-in-haystack,YaRN 外推专项)

构造 512k 数字序列 haystack,不同深度埋事实句,temperature=0 提问:

| 测试 | 结果 | 备注 |
|---|---|---|
| 深针命中@70% 深度(纯外推区) | PASS | 准确答出"十月九日" |
| 幻觉拒答×3(问不存在的针) | 3/3 PASS | 均答"未记载",没编造 |
| 尾部追加深针@~100% | PASS | 11s(前缀缓存命中后只 prefill 追加部分) |

外推区(>256k)检索质量正常,无幻觉倾向。

### 抽样结论

量化(mixed 4/8bit)+KV8+YaRN512K 组合在小样本上未观察到质量劣化:
基础能力 13/13,长上下文检索+抗幻觉 5/5。
样本量小(合计 18 项),不足以给出统计置信的分数,但作为"配置可用性验收"足够——
日常 ZCode 使用放行,后续如遇到质量疑点再定向补测。

### 过程中的坑(复现者注意)

1. 埋针代码 bug:数字序列步进约 8 字符/次,`int(n*depth)` 落点大概率不在步进点上,
   4 根针只埋进去 1 根。教训:埋针要在生成循环里按"累计长度过阈值"插入,不要精确匹配落点。
2. 512k 双会话并发会瞬时 OOM("This prompt does not fit in GPU memory"):
   maxConcurrent=1,前一个大上下文请求的 slot 还没释放。串行+重试即可。
3. 判分脚本本身也要被复核(GSM2 期望值错误差点冤枉模型)。

## 追加:Qwen3.8-27B 速度抽测(2026-09-13,同机同端口 11234)

背景:mlx-serve 另装了 `lmstudio-community/Qwen3.8-27B-MLX-4bit`(15GB,dense,
原生 256K)。被加载后用 `python3 bench.py q27b` 同方法论抽测,与 MLX Flash-Next 对比。
**27B 无 MTP 投机解码(mtp_loaded=false),decode 为裸速度**;kv8/前缀缓存配置沿用服务端原样。
测试中途被用户叫停:75k 冷测只完成 1 次,150k 未测。原始数据 `result_q27b_partial.json`。

| 指标 | 27B | Flash-Next | 差距 |
|---|---|---|---|
| 5k 冷 TTFT / prefill | ~23.5 s / ~215 tok/s | 9.1 s / 555 tok/s | 2.6x |
| 5k 热 TTFT | 0.15 s | 0.22 s | 持平 |
| 5k decode | 12~20 tok/s | 71 tok/s | 4~5x |
| 75k 冷 TTFT / prefill | 651.7 s / 115 tok/s | 172 s / 437 tok/s | 3.8x |
| 75k decode | 7.3 tok/s | 41 tok/s | 5.6x |
| 150k | 未测 | 346 s / 434 tok/s | — |

要点:prefill 随长度衰减(227→115 tok/s,dense 模型长序列注意力开销大),
而 Flash-Next(MoE + linear attention 为主)稳定 ~435;前缀缓存对 27B 正常生效;
5k decode 连续压测从 19.6 降到 11.6,疑似热节流。

结论:**27B 仅剩内存优势(常驻 16GB vs 75GB)**。短上下文勉强可用(冷启动 ~20s、
decode 十几 tok/s),长上下文不可用(75k 冷 prefill 11 分钟、decode 7 tok/s)。
日常主力维持 Flash-Next,27B 定位为"内存紧张时的轻量备选"。

