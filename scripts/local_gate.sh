#!/bin/bash
# 本機閘(CHG-20261108-01 T4;AM-11):Python 全量閘四段 + tests/parity,結果寫入 artifacts/gate/<sha>.json。
#
#   scripts/local_gate.sh
#
# 必須前景執行(含 SIGINT 類測試,不可放到背景);全程超過 10 分鐘。任一段失敗就以非 0 結束,但每一段都會跑完並回報。
# 終端輸出只有測試 id、狀態與計數,不轉印失敗內容(可能含秘密;spec 15.10.4);要看細節時單獨重跑該測試。
# 本筆沒有 Rust 程式,這裡只驗 Python 版;報告以 rust_side.exists = false 明寫。
set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd) || exit 2
cd "$ROOT" || exit 2
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "local_gate: 找不到 $PY" >&2
  exit 2
fi
SHA=$(git rev-parse HEAD) || exit 2
START_STATUS=$(git status --porcelain) || exit 2
if [ -n "$START_STATUS" ]; then DIRTY=1; else DIRTY=0; fi
# 報告只來自這一次執行:中途中止或彙整被拒時,不留下同一 commit 的舊結論
rm -f "artifacts/gate/$SHA.json"

export PYTHONPATH="$ROOT/src" LITELLM_LOCAL_MODEL_COST_MAP=True
for name in SPILLWAY_CMD SPILLWAY_IMPL SPILLWAY_PARITY_CMD_B SPILLWAY_BLACKBOX_GUARD PYTEST_ADDOPTS PYTEST_PLUGINS; do
  if [ -n "${!name:-}" ]; then echo "local_gate: 清除環境變數 $name(受測範圍不受呼叫端環境影響)"; fi
  unset "$name"
done

# 各段的結果檔(behave json 含失敗內容)只放暫存目錄,結束時刪除;artifacts/ 只留彙整後的報告。
WORK=$(mktemp -d "${TMPDIR:-/tmp}/spillway-gate.XXXXXX") || exit 2
trap 'rm -rf "$WORK"' EXIT
PARTS="$WORK/parts.tsv"
: >"$PARTS"

record() {  # 段名 種類 結束碼 秒數 結果檔
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >>"$PARTS"
  echo "== $1: exit=$3 seconds=$4"
}

run_pytest() {  # 段名 pytest 參數...
  local name=$1 out="$WORK/$1.json" start=$SECONDS rc
  shift
  # --tb=no -rN:只留進度與總計,失敗內容與 short summary 都不印
  "$PY" -m pytest -q --tb=no -rN -p no:cacheprovider -p tests.parity.gate_report "--gate-results=$out" "$@" 2>&1 | tail -3
  rc=${PIPESTATUS[0]}
  record "$name" pytest "$rc" "$((SECONDS - start))" "$out"
}

run_behave() {  # 段名 feature 根目錄
  local name=$1 root=$2 out="$WORK/$1.json" start=$SECONDS rc_behave rc_gate rc
  # behave 自己的輸出含失敗內容,丟棄;判定看 behave_gate(只印 id 與狀態)與 behave 的結束碼(after_all 不在 json 內)
  "$PY" -m behave "$root" -f json -o "$out" >/dev/null 2>&1
  rc_behave=$?
  "$PY" -m tests.parity.behave_gate "$out" --root "$root" 2>&1
  rc_gate=$?
  rc=$rc_behave
  if [ "$rc" -eq 0 ]; then rc=$rc_gate; fi
  record "$name" behave "$rc" "$((SECONDS - start))" "$out"
}

run_compare() {
  local start=$SECONDS rc
  "$PY" artifacts/m5a/baseline/compare.py --behave-json "$WORK/behave_features.json" 2>&1 | tail -15
  rc=${PIPESTATUS[0]}
  record compare plain "$rc" "$((SECONDS - start))" ""
}

echo "local_gate: HEAD $SHA"
# Python 全量閘四段(CHG-20261107-03)
run_pytest pytest_full
# shellcheck disable=SC2046  # 目錄清單依空白切開
SPILLWAY_BLACKBOX_GUARD=1 run_pytest pytest_guard tests/blackbox $(grep -v -e '^#' -e '^$' tests/blackbox/converted_roots.txt)
run_behave behave_features tests/features
run_compare
# parity
run_pytest pytest_parity tests/parity
run_behave behave_parity tests/parity/features

# 全程超過 10 分鐘:期間 HEAD 或工作目錄有變動時,報告不能代表 $SHA
if [ "$(git rev-parse HEAD)" != "$SHA" ] || [ "$(git status --porcelain)" != "$START_STATUS" ]; then
  echo "local_gate: 執行期間 HEAD 或工作目錄有變動"
  DIRTY=1
fi
"$PY" -m tests.parity.gate_report --sha "$SHA" --dirty "$DIRTY" --parts "$PARTS" --out "artifacts/gate/$SHA.json"
exit $?
