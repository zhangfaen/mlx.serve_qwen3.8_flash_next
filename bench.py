#!/usr/bin/env python3
"""Qwen3.8 Flash Next 本地双引擎对比 benchmark:LM Studio vs MLX Serve。

测什么:冷 prefill 吞吐(真实首次计算)、热 TTFT(前缀缓存命中)、decode 吞吐、上下文长度对 decode 的影响。

关键设计决策(为什么这么做):
1. 冷/热分开测。MLX 和 llama.cpp 都有前缀缓存,重复同 payload 会命中缓存得到 0.1s 的假 TTFT。
   冷测 = 每次换随机种子生成不同数字序列,强制服务端做完整 prefill;
   热测 = 紧接着重发同一 payload,测缓存命中后的响应速度。
   场景例子:ZCode 里每轮对话都重发"系统提示+历史+新内容",前缀大部分命中缓存 → 热 TTFT 才是日常体感;
   冷 prefill 对应新开会话/缓存被挤掉后的第一枪等待。
2. 输出任务固定("复述前100个数字"),两引擎生成内容一致,排除"模型聪明度不同导致输出长度不同"的污染。
3. token 数以服务端 usage 为准(两边的 tokenizer 同源,数量应基本一致,可互为校验)。
"""
import http.client
import json
import random
import statistics
import sys
import time
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).parent

PROVIDERS = {
    "lmstudio": {
        "host": "127.0.0.1",
        "port": 1234,
        "api_key": "lm-studio",
        "model": "qwen3.8-flash-next",
    },
    "mlx": {
        "host": "127.0.0.1",
        "port": 11234,
        "api_key": "mlx-serve",
        "model": "ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit",
    },
}

# 场景:(名字, 目标字符数≈token数, 冷测次数, 热测次数)
# 数字序列 ~1 token/字符(S2 实测 74430 字符 → 74479 token)
SCENARIOS = [
    ("S1_5k",   5000,   2, 2),
    ("S2_75k",  75000,  2, 2),
    ("S3_150k", 150000, 2, 2),
]

INSTRUCTION = (
    "下面是一串数字序列。请原样复述前 100 个数字,用逗号分隔,"
    "然后立刻输出 END,不要输出任何其他内容,不要思考。\n\n"
)


def gen_digits(n_chars, seed):
    """生成指定字符数的随机数字序列。seed 不同 → 内容不同 → 服务端无法命中前缀缓存。"""
    rng = random.Random(seed)
    parts = []
    total = 0
    while total < n_chars:
        s = str(rng.randint(0, 999999))
        parts.append(s)
        total += len(s) + 2
    return ", ".join(parts)


def one_request(provider, prompt_text, max_tokens=1024):
    """发一次流式请求,返回计时数据。HTTP 错误时把响应体带出来(方便诊断 400 的真实原因)。"""
    cfg = PROVIDERS[provider]
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": INSTRUCTION + prompt_text}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    # 为什么不用 urllib:LM Studio 在长 prompt prefill 期间不发送任何字节,
    # urllib 的 resp.readline() 会阻塞到首行才返回(实测 5 分钟 prefill 会卡死读循环)。
    # http.client 只要连接建立就能拿到响应头,后续逐行读不会被"慢启动 SSE"坑住。
    # 注意:HTTPConnection 的 path 参数只要路径部分;之前把完整 URL 传进去,
    # 实际请求了 http://127.0.0.1:1234http://... 导致 LM Studio 返回空响应。
    host, port = cfg["host"], cfg["port"]
    conn = http.client.HTTPConnection(host, int(port), timeout=1800)
    conn.request(
        "POST",
        "/v1/chat/completions",
        body=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
    )

    t_start = time.perf_counter()
    t_first = None
    t_last = None
    n_chunks = 0
    usage = None
    finish_reason = None

    try:
        resp = conn.getresponse()
        if resp.status != 200:
            detail = resp.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"HTTP {resp.status}: {detail}")
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            choices = obj.get("choices") or []
            if choices:
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
                delta = choices[0].get("delta") or {}
                # LM Studio 会输出 reasoning_content(思考内容),且忽略"不要思考"指令。
                # 统一口径:reasoning_content 和 content 都算生成 token,TTFT 取二者先到者,
                # 否则 LM Studio 思考几分钟会被误判为"无响应"。
                if delta.get("content") or delta.get("reasoning_content"):
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                    t_last = now
                    n_chunks += 1
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    finally:
        conn.close()

    t_end = time.perf_counter()
    if t_first is None:
        raise RuntimeError("no content chunk received")

    gen_time = t_end - t_first
    comp_tokens = (usage or {}).get("completion_tokens") or n_chunks
    prompt_tokens = (usage or {}).get("prompt_tokens") or 0
    return {
        "ttft_s": round(t_first - t_start, 3),
        "gen_s": round(gen_time, 3),
        "completion_tokens": comp_tokens,
        "prompt_tokens": prompt_tokens,
        "finish_reason": finish_reason,
        "decode_tps": round(comp_tokens / gen_time, 1) if gen_time > 0 else None,
    }


def run_scenario(provider, name, n_chars, n_cold, n_warm):
    """冷测:每个 seed 生成全新内容(强制完整 prefill);热测:紧跟着重发同一段。"""
    results = []

    for i in range(n_cold):
        payload = gen_digits(n_chars, seed=1000 + i)
        for attempt in range(2):
            try:
                r = one_request(provider, payload)
                r["kind"] = "cold"
                results.append(r)
                print(f"  [{provider}] {name} cold{i+1}: TTFT={r['ttft_s']}s "
                      f"ptok={r['prompt_tokens']} ctok={r['completion_tokens']} "
                      f"decode={r['decode_tps']} finish={r['finish_reason']}", flush=True)
                break
            except Exception as e:
                print(f"  [{provider}] {name} cold{i+1} attempt{attempt+1} FAILED: {e}", flush=True)
                time.sleep(3)

    if results:
        warm_payload = gen_digits(n_chars, seed=9999)  # 全新 seed,先冷一次再热
        cold = one_request(provider, warm_payload)
        cold["kind"] = "cold(warmup)"
        results.append(cold)
        print(f"  [{provider}] {name} warmup-cold: TTFT={cold['ttft_s']}s", flush=True)
        for i in range(n_warm):
            r = one_request(provider, warm_payload)
            r["kind"] = "warm"
            results.append(r)
            print(f"  [{provider}] {name} warm{i+1}: TTFT={r['ttft_s']}s "
                  f"decode={r['decode_tps']} finish={r['finish_reason']}", flush=True)

    return {"scenario": name, "provider": provider, "n_chars": n_chars, "runs": results}


def summarize(all_results):
    print("\n=== summary (median) ===")
    print(f"{'scenario':<10} {'cold_TTFT':>10} {'cold_prefill':>13} {'warm_TTFT':>10} {'decode_tps':>11} {'ptok':>8}")
    for s in all_results:
        cold = [r for r in s["runs"] if r["kind"] == "cold"]
        warm = [r for r in s["runs"] if r["kind"] == "warm"]
        all_decode = [r["decode_tps"] for r in s["runs"] if r.get("decode_tps")]
        if not s["runs"]:
            print(f"{s['scenario']:<10} ALL FAILED")
            continue
        cold_ttft = statistics.median([r["ttft_s"] for r in cold]) if cold else None
        ptok = statistics.median([r["prompt_tokens"] for r in s["runs"] if r["prompt_tokens"]])
        prefill = round(ptok / cold_ttft, 1) if cold_ttft and ptok else None
        warm_ttft = statistics.median([r["ttft_s"] for r in warm]) if warm else None
        decode = statistics.median(all_decode) if all_decode else None
        print(f"{s['scenario']:<10} "
              f"{cold_ttft or '-':>10} {str(prefill or '-'):>13} {str(warm_ttft or '-'):>10} "
              f"{str(decode or '-'):>11} {int(ptok) if ptok else '-':>8}")


def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else "mlx"
    out_file = BASE_DIR / f"result_{provider}_v2.json"
    print(f"=== benchmark v2 provider={provider} ===", flush=True)
    all_results = []
    for name, n_chars, n_cold, n_warm in SCENARIOS:
        print(f"-- scenario {name} (~{n_chars} tok)", flush=True)
        all_results.append(run_scenario(provider, name, n_chars, n_cold, n_warm))
    out_file.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    print(f"\nsaved -> {out_file}")
    summarize(all_results)


if __name__ == "__main__":
    main()
