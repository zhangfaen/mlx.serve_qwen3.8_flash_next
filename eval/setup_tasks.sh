#!/bin/bash
# 构建 agentic 评测任务包:每个任务 = 真实开源仓库回滚到 bugfix 前 + 打上该 fix 的测试补丁。
# 判分标准:子代理修好后,指定测试文件必须全绿;构建时先验证"未修复时确实 fail"。
# 用法: bash setup_tasks.sh   (脚本自定位到 eval/ 目录)
set -e
cd "$(dirname "$0")"

# 任务表: 任务名|源仓库目录|fix提交|测试文件(用于跑 pytest 的入口)
tasks="
dotenv-1|python-dotenv|f5485a6|tests/test_parser.py tests/test_main.py
dotenv-2|python-dotenv|bca6644|tests/test_parser.py tests/test_main.py
semver-1|python-semver|0aa17a3|tests/test_compare_invalid_operands.py
semver-2|python-semver|4e09ef0|tests/test_parsing.py
humanize-1|humanize|823ad60|tests/test_filesize.py
humanize-2|humanize|a47a89e|tests/test_time.py
"

echo "$tasks" | while IFS='|' read -r name srcrepo fix testfiles; do
  [ -z "$name" ] && continue
  taskdir="tasks/$name"
  if [ -d "$taskdir" ]; then echo "== $name 已存在,跳过"; continue; fi
  echo "== 构建 $name ($srcrepo @ $fix^)"
  # 1. 干净克隆,在 fix 的父提交上开一个 work 分支
  git clone -q ".cache/repos/$srcrepo" "$taskdir"
  git -C "$taskdir" checkout -q -b work "$fix^"
  # 2. 抹掉一切能追溯 fix 提交的痕迹:origin、main 分支、reflog、不可达对象。
  #    防作弊要点:子代理若跑 git log --all 会直接看到官方修法,必须 gc 掉。
  git -C "$taskdir" remote remove origin
  # 默认分支名各仓库不同(main/master),通用地删掉除 work 以外的所有本地分支
  git -C "$taskdir" for-each-ref --format='%(refname:short)' refs/heads | grep -v '^work$' | xargs -r -I{} git -C "$taskdir" branch -D {} >/dev/null
  git -C "$taskdir" reflog expire --expire=now --all
  git -C "$taskdir" gc --prune=now --quiet
  # 3. 应用 fix 提交里 tests/ 部分的改动(测试补丁),提交为"验收测试"
  git -C ".cache/repos/$srcrepo" show "$fix" -- tests/ > "$taskdir/.test-patch.diff"
  git -C "$taskdir" apply ".test-patch.diff"
  git -C "$taskdir" add -A
  git -C "$taskdir" commit -qm "test: add acceptance tests for reported bug"
  # 4. 独立 venv + 安装被测包和测试依赖
  uv venv "$taskdir/.venv" -q
  uv pip install -q -p "$taskdir/.venv/bin/python" -e "$taskdir" pytest pytz click
  # 5. 验证:未修复状态下,验收测试必须产生 fail(任务真实有效)
  echo "-- 验证 $name 初始状态应为 fail:"
  (cd "$taskdir" && .venv/bin/python -m pytest $testfiles -q 2>&1 | tail -2) || true
done
