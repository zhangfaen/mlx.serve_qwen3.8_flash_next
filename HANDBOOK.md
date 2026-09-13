# Mac 本地跑 Qwen3.8 Flash Next 综合手册

> 基于 2026-09-13 的实测整理。涵盖:mlx-serve 安装配置全流程、512K 上下文调优、
> 与 LM Studio 的速度对比实测、准确率抽样验收、ZCode 接入配置、常见坑与回滚方法。
> 目标:未来拿着这份文档可以从零复现整套环境,也能查到当时测出的每一个数据。

---

## 1. 环境与选型结论

### 硬件

- MacBook Pro (Mac15,9), Apple M3 Max, 128GB 统一内存, 16 核 CPU
- 磁盘:模型目录占 100GB,务必预留

### 被测对象与最终选择

| | LM Studio | **MLX Serve(胜出,主用)** |
|---|---|---|
| 引擎 | llama.cpp (GGUF) | MLX |
| 模型 | unsloth Qwen3.8-Flash-Next UD-Q3_K_XL | ddalcu mixed 4/8bit |
| 端口 | 1234 | **11234** |
| Decode 吞吐(75k ctx) | 13 tok/s | **41 tok/s(3.2x)** |
| 冷 prefill(75k) | 418 s | **172 s** |
| 常驻内存 | 83.8 GB | **75.3 GB** |
| 上下文(调优后) | 200k | **512k(YaRN 外推)** |

选 MLX Serve 的理由:decode 全场景 3~5 倍快(ZCode 体感的主要来源)、长上下文
prefill 2 倍快且不随长度衰减、常驻内存更小、量化更高(4/8bit vs Q3)。
LM Studio 保留作备用(它上面还有 embedding、图像等其它模型)。

---

## 2. mlx-serve 从零安装

### 2.1 两种安装形态

- **MLX Core.app(图形界面,当前在用)**:托盘管理模型加载/卸载,设置界面改参数。
  下载:`https://github.com/ddalcu/mlx-serve/releases` → `MLXCore.dmg`
- **纯 CLI 二进制**:同页面 `mlx-serve-bin-macos-arm64.tar.gz`,解压即用,
  适合无界面跑法。App 内 `mlx-serve` 本体就是同一个二进制。

版本:本文全部实测基于 **v26.9.2**(2026-09-09 发布)。

### 2.2 下载模型

模型来自 HuggingFace:`ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit`
(mixed 4/8bit 量化,磁盘 100GB)。默认目录 `~/.mlx-serve/models/`。

```bash
# 走国内镜像(网络代理时用 huggingface 直连)
export HF_ENDPOINT=https://hf-mirror.com
huggingface-cli download ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit \
  --local-dir ~/.mlx-serve/models/ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit
```

注意:首次启动时 MLX Core 也可以在界面里直接下载模型(支持镜像源设置),殊途同归。

### 2.3 启动参数(App 生成,等价 CLI)

App 从 plist 读设置拼命令行。关键参数及推荐值:

```bash
/Applications/MLX\ Core.app/Contents/MacOS/mlx-serve \
  --model ~/.mlx-serve/models/ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit \
  --serve --port 11234 --host 127.0.0.0 \
  --ctx-size 524288 \
  --kv-quant 8 \
  --mtp \
  --pld --pld-draft-len 5 --pld-key-len 3 \
  --prefix-cache-entries 16 \
  --prefix-cache-mem 28GB \
  --prefix-cache-disk 100GB \
  --metrics
```

参数含义(为什么这么配):

| 参数 | 值 | 理由 |
|---|---|---|
| `--ctx-size` | 524288 | 512K 窗口,见 §3 YaRN 说明 |
| `--kv-quant` | 8 | KV cache 8bit。512K×4会话的 KV 全 fp16 要 50GB,8bit 后 25GB 放得下;实测 decode 仅轻微变慢(75k: 41→35 tok/s 量级) |
| `--mtp` | 开 | 多 token 预测投机解码,decode 3~5 倍优势的主要来源 |
| `--prefix-cache-mem` | 28GB | 前缀缓存内存预算。默认仅 2GB,150k 上下文就部分失效;28GB 可全量容 4 个 512K 会话(kv8 下每会话≈6.3GB) |
| `--prefix-cache-entries` | 16 | LRU 条目数上限(默认 32,原配置被手动压到 8 过) |
| `--prefix-cache-disk` | 100GB | SSD 层:内存挤掉的前缀落盘,重启恢复,不再重算 |
| `--pld*` | 开 | 投机解码的 draft 配置(默认即可) |

### 2.4 App 界面等价配置(serverOptions)

App 设置界面对应 plist `com.dalcu.mlx-core` 的 `serverOptions` JSON:

```
ctxSize=524288  kvQuant=8  prefixCacheEntries=16
prefixCacheMem=28GB  enablePrefixCacheDisk=true  prefixCacheDisk=100GB
port=11234  host=0.0.0.0  enableMTP=true  enablePLD=true
```

---

## 3. 512K 上下文(YaRN 外推)

### 3.1 背景

Qwen3.8-Flash-Next 原生 `max_position_embeddings=262144`(256K)。
Qwen 官方 README 明确支持 YaRN 外推:"extensible up to 1,000,000 tokens",
并建议 512K 用 factor=2.0,1M 用 factor=4.0。

架构上对长上下文友好:48 层中仅 12 层 full attention(其余 linear attention,
状态恒定),KV 占用 ≈24KB/token(fp16)/≈12KB(kv8)。

### 3.2 落地方式

App 设置界面**没有** `--config-overrides` 项,所以 YaRN 直接改模型文件
(`--config-overrides` 本质就是 deep-merge 进这份 config):

`~/.mlx-serve/models/ddalcu/.../config.json` 的 `text_config` 中:

```json
{
  "max_position_embeddings": 524288,
  "rope_parameters": {
    "mrope_interleaved": true,
    "mrope_section": [11, 11, 10],
    "rope_type": "yarn",
    "rope_theta": 10000000,
    "partial_rotary_factor": 0.25,
    "factor": 2.0,
    "original_max_position_embeddings": 262144
  }
}
```

其余字段(quantization、layer_types、vision_config 等)保持不动。

### 3.3 验证结果

- 300k token 请求(超原生 256K)正常处理:冷 prefill 857s(~350 tok/s,
  与 256K 内速度一致,外推无速度惩罚),decode 35.6 tok/s
- 512k haystack 检索:70% 深度针命中、3 次幻觉拒答全过、尾部深针命中
- **取舍说明**:1M(factor 4.0)没开——4 会话×1M 即使 kv8 也要 ~50GB,加权重
  75GB 顶死 128GB;且 factor 4.0 的外推折损更大。512K×4 会话是这台机器的甜点位。

---

## 4. 速度实测数据(2026-09-13)

方法:数字序列复述任务,temp=0,流式逐 chunk 计时,服务端 usage 为 token 依据;
冷测每轮换随机 seed 强制完整 prefill,热测重发同 payload;
每场景冷 2 次+热 2 次取中位数;两引擎独占内存顺序测试。
脚本:`bench.py`(同目录,`python3 bench.py mlx|lmstudio`)。

### TTFT(秒,越小越好)

| 场景 | LM Studio 冷 | MLX 冷 | LM Studio 热 | MLX 热 |
|---|---|---|---|---|
| 5k tok | 13.0 | 9.1 | 0.11 | 0.22 |
| 75k tok | 418 | 172 | 0.16 | 0.29 |
| 150k tok | 721 | 346 | 0.21 | 162⚠️ |

⚠️ MLX 150k 热测 162s 是旧配置(2GB 缓存)下前缀不能全量命中;
**调优 28GB 后复测:300k 前缀 TTFT 857s→5.7s 全量命中**。

### Prefill 吞吐(tok/s,越大越好)

| 场景 | LM Studio | MLX |
|---|---|---|
| 5k | 775 | 555 |
| 75k | 311 | 437 |
| 150k | 208 | 434 |

LM Studio 随长度急剧衰减;MLX 长文本稳定 ~435。

### Decode 吞吐(tok/s,越大越好)

| 上下文 | LM Studio | MLX |
|---|---|---|
| 5k | 21 | 71 |
| 75k | 13 | 41 |
| 150k | 8.8 | 41 |

### 其他

- 冷启动(加载模型):LM Studio 29.2s / MLX 9.7s(进程已在,API 热加载)
- 常驻内存:LM Studio 83.8GB / MLX 75.3GB
- CPU:两引擎计算全在 GPU,prefill/decode 期进程 CPU <0.1 核
- 体感换算(150k ctx 写 1000 token):LM Studio 11.5 分钟 / MLX 冷 6.2 分钟,
  缓存命中 24 秒

---

## 5. 准确率抽样验收(2026-09-13)

全量基准太耗时,按"抽样验收"执行,合计 18 项全过:

| 维度 | 结果 | 说明 |
|---|---|---|
| 代码生成(HumanEval 风格×5,沙箱单测) | 5/5 | fizzbuzz/Kadane/括号匹配等 |
| 数学推理(GSM8K 风格×5) | 5/5 | 1 题首判 FAIL 是脚本期望值错,模型对 |
| 指令遵循(IFEval 风格×3) | 3/3 | 恰好3句话/单行无注释/大写翻译 |
| 512k 深针检索(needle-in-haystack) | 1/1 | 70% 深度(纯外推区)命中 |
| 幻觉拒答(问不存在的针×3) | 3/3 | 均答"未记载"未编造 |
| 尾部追加深针 | 1/1 | 11s,增量 prefill 只算新增段 |

结论:量化+kv8+YaRN512K 组合未见质量劣化,日常放行。
样本量小不构成统计分数,后续遇质量疑点再定向补测。

---

## 6. ZCode 接入配置

`~/.zcode/v2/config.json` 的 provider 段(已生效):

```json
"ad2bf3dd-1834-4ec1-a1e2-e0e7f0fd56b5": {
  "name": "MLX Serve Local",
  "kind": "openai-compatible",
  "options": {
    "apiKey": "mlx-serve",
    "baseURL": "http://127.0.0.1:11234/v1",
    "apiKeyRequired": true
  },
  "source": "custom",
  "models": {
    "ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit": {
      "name": "Qwen3.8 Flash Next (MLX Serve 4/8bit)",
      "reasoning": { "enabled": true,
        "variants": ["low", "medium", "xhigh"], "defaultVariant": "medium" },
      "limit": { "context": 524288, "output": 32768 },
      "modalities": { "input": ["text", "image", "video"], "output": ["text"] }
    }
  }
}
```

要点:`limit.context` 必须与服务端 `--ctx-size` 一致(524288),
否则客户端提前截断,512K 白开。改完重启 ZCode 生效。

---

## 7. 日常运维

### 常用操作

```bash
# 服务健康/模型状态(loaded/state/kv_quant/ctx 都能看到)
curl -s http://127.0.0.1:11234/v1/models | python3 -m json.tool

# 手动卸载/加载模型(App 托盘也可)
curl -s -X POST http://127.0.0.1:11234/v1/unload-model -d '{}' -H 'Content-Type: application/json'
curl -s -X POST http://127.0.0.1:11234/v1/load-model \
  -d '{"model":"ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit"}' -H 'Content-Type: application/json'

# Prometheus 指标(启动带 --metrics)
curl -s http://127.0.0.1:11234/metrics | grep -iE "prefix|cache" | head

# 服务日志位置
~/.mlx-serve/logs/
```

### 备份与回滚(本次调优留下的)

| 内容 | 备份位置 |
|---|---|
| 模型 config.json(256K 原版) | `~/.mlx-serve/models/ddalcu/.../config.json.bak-256k` |
| App plist(调优前) | `~/Library/Preferences/com.dalcu.mlx-core.plist.bak-20260913152507` |

回滚:两个备份复制回原位(plist 用 `defaults import` 或直接覆盖后重启 App)。

### 内存预算表(128GB 机器)

| 项 | 占用 |
|---|---|
| 模型权重常驻 | 75.3 GB |
| 每个活跃 512K 会话 KV(kv8) | ≈6.3 GB |
| 前缀缓存预算 | 28 GB(≈4 会话) |
| 系统及其它 | 剩余 ~15 GB |

同时只跑一个大模型;LM Studio 的 84GB 模型加载前必须先卸载 MLX 侧模型(反之亦然)。

---

## 8. 坑与经验(复现者必读)

1. **前缀缓存默认 2GB 形同虚设**:150k 上下文就部分 miss。
   `--prefix-cache-mem` 按"会话数×6.3GB(kv8 512K)"估算调大。
2. **SSE 慢启动会卡死 urllib**:LM Studio 长 prefill 期间不发首行,
   `urllib.request` 卡在 readline。测速脚本用 `http.client`。
3. **LM Studio 忽略"不要思考"**:持续输出 `reasoning_content`,判分/计时脚本
   要把它算进输出 token,否则误判"无响应"。
4. **maxConcurrent=1 时并发大请求瞬时 OOM**("does not fit in GPU memory"):
   前一个大上下文 slot 未释放。串行+重试。
5. **needle-in-haystack 埋针别用精确落点**:数字序列步进会跳过
   `int(n*depth)` 落点,按"累计长度过阈值"插入。
6. **判分脚本本身要复核**:本次 GSM2 期望值算错差点冤枉模型。
7. **改 plist 前先退出 App**:App 退出时可能用内存旧值覆盖写回。
8. **1M 慎开**:factor 4.0 折损更大且 4 会话内存放不下,512K 是甜点位。

---

## 9. 目录与文件索引

| 文件 | 内容 |
|---|---|
| `~/qwen-bench 手册同目录/bench.py` | 速度基准脚本(冷/热 prefill、decode、TTFT) |
| `result_mlx_v2.json` / `result_lmstudio_v2.json` | 速度测试原始数据 |
| `result_yarn_needle.json` / `haystack_512k.txt` | 512K 检索测试数据与 payload |
| `~/.mlx-serve/models/ddalcu/.../config.json` | 模型配置(YaRN 已启用) |
| `~/Library/Preferences/com.dalcu.mlx-core.plist` | MLX Core App 设置 |
| `~/.zcode/v2/config.json` | ZCode provider 配置 |
