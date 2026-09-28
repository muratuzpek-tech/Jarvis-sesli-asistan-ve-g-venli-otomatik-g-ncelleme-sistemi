#!/usr/bin/env bash
# kontrol_et.sh — JARVIS reposunu tek komutla tarar ve GÜVENLİ düzeltmeleri uygular.
#
# Ne yapar (sırayla):
#   1. Çalışma ağacı temiz mi kontrol eder (commit edilmemiş değişikliğin varken
#      çalışmaz — böylece her şey tek komutla geri alınabilir kalır).
#   2. Ayrı bir dal açar: otomatik-duzeltme-YYYYAAGG-SSDD
#   3. Testleri çalıştırıp BAŞLANGIÇ durumunu ölçer.
#   4. ruff ile yalnızca "güvenli" otomatik düzeltmeleri uygular.
#   5. Testleri tekrar çalıştırır. Sonuç başlangıçtan KÖTÜYSE ruff değişikliklerini
#      geri alır; değilse değişiklikleri o dalda commit eder.
#   6. (İsteğe bağlı, --aider) Kalan ruff bulgusu olan küçük dosyaları tek tek
#      Aider + yerel Ollama modeline düzelttirir; her dosyadan sonra testler
#      kötüleşirse o dosyanın değişikliğini geri alır.
#   7. Raporu .jarvis_kontrol/ altına yazar ve özet gösterir.
#
# Kullanım:
#   bash scripts/kontrol_et.sh              # ruff + testler
#   bash scripts/kontrol_et.sh --aider      # + kalan bulgular için Aider
#   bash scripts/kontrol_et.sh --aider --model ollama_chat/qwen2.5-coder:7b
#
# Hiçbir şey ana dala (main) yazılmaz. Beğenmezsen:
#   git checkout main && git branch -D <dal-adı>
set -euo pipefail

# ── ayarlar ──────────────────────────────────────────────────────────────
USE_AIDER=0
AIDER_MODEL="ollama_chat/qwen2.5-coder:14b"
AIDER_MAX_FILES=5                 # tek çalıştırmada en fazla bu kadar dosya
AIDER_MAX_BYTES=60000             # bundan büyük dosyalar yerel modele verilmez
TEST_TIMEOUT=900                  # saniye
TARGETS=(src tests)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --aider) USE_AIDER=1 ;;
    --model) shift; AIDER_MODEL="${1:?--model bir değer ister}" ;;
    --max-files) shift; AIDER_MAX_FILES="${1:?--max-files bir sayı ister}" ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Bilinmeyen seçenek: $1 (yardım: --help)" >&2; exit 2 ;;
  esac
  shift
done
[[ "$AIDER_MAX_FILES" =~ ^[0-9]+$ ]] || { echo "--max-files bir sayı olmalı" >&2; exit 2; }

# ── yardımcılar ──────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
REPORT_DIR="$ROOT/.jarvis_kontrol"
mkdir -p "$REPORT_DIR"
STAMP="$(date +%Y%m%d-%H%M)"
LOG="$REPORT_DIR/rapor-$STAMP.txt"
: > "$LOG"

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
head_() { printf '\n== %s ==\n' "$*" | tee -a "$LOG"; }
die()  { say "HATA: $*"; exit 1; }

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "Python bulunamadı."

if [[ -x "$ROOT/.venv/bin/ruff" ]]; then RUFF=("$ROOT/.venv/bin/ruff")
elif command -v ruff >/dev/null 2>&1; then RUFF=(ruff)
elif "$PY" -m ruff --version >/dev/null 2>&1; then RUFF=("$PY" -m ruff)
else die "ruff bulunamadı. Kurmak için: $PY -m pip install ruff"
fi

# Testleri çalıştırır; "başarısız+hatalı" test sayısını stdout'a yazar.
# pytest hiç çalışamazsa (ör. toplama hatası) çok büyük bir sayı döner.
count_failures() {
  local out rc=0
  out="$(timeout "$TEST_TIMEOUT" "$PY" -m pytest -q -p no:cacheprovider tests 2>&1)" || rc=$?
  printf '%s\n' "$out" | tail -n 15 >> "$LOG"
  if [[ $rc -eq 124 ]]; then echo 99999; return; fi
  local failed errors
  failed="$(grep -oE '[0-9]+ failed' <<<"$out" | tail -1 | grep -oE '[0-9]+' || echo 0)"
  errors="$(grep -oE '[0-9]+ errors?' <<<"$out" | tail -1 | grep -oE '[0-9]+' || echo 0)"
  if [[ $rc -ne 0 && $failed -eq 0 && $errors -eq 0 ]]; then echo 99999; return; fi
  echo $((failed + errors))
}

# ── 1-2. güvenli başlangıç ───────────────────────────────────────────────
head_ "Hazırlık"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Bu klasör bir git deposu değil."
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  git status --short --untracked-files=no | tee -a "$LOG"
  die "Commit edilmemiş değişiklikler var. Önce commit et (ya da git stash), sonra tekrar çalıştır."
fi
START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BRANCH="otomatik-duzeltme-$STAMP"
git checkout -q -b "$BRANCH"
say "Dal: $BRANCH (başlangıç: $START_BRANCH)"
say "Python: $PY | ruff: $("${RUFF[@]}" --version)"

# ── 3. başlangıç ölçümü ──────────────────────────────────────────────────
head_ "Başlangıç test durumu"
BASE_FAIL="$(count_failures)"
say "Başarısız test (başlangıç): $BASE_FAIL"
BASE_RUFF="$("${RUFF[@]}" check "${TARGETS[@]}" --output-format concise --exit-zero | grep -cE '^[^ ].+:[0-9]+:[0-9]+:' || true)"
say "ruff bulgusu (başlangıç): $BASE_RUFF"

# ── 4-5. ruff güvenli düzeltmeler ────────────────────────────────────────
head_ "ruff güvenli düzeltmeler"
"${RUFF[@]}" check "${TARGETS[@]}" --fix --exit-zero --quiet >> "$LOG" 2>&1 || true
CHANGED="$(git diff --name-only)"
if [[ -z "$CHANGED" ]]; then
  say "ruff'ın otomatik düzeltebileceği bir şey yok."
else
  git diff --stat | tee -a "$LOG"
  AFTER_FAIL="$(count_failures)"
  say "Başarısız test (ruff sonrası): $AFTER_FAIL"
  if (( AFTER_FAIL > BASE_FAIL )); then
    git checkout -q -- .
    say "⚠️ ruff düzeltmeleri testleri kötüleştirdi → GERİ ALINDI."
  else
    git commit -q -am "ruff: güvenli otomatik düzeltmeler (kontrol_et.sh)"
    BASE_FAIL="$AFTER_FAIL"
    say "✅ ruff düzeltmeleri commit edildi."
  fi
fi

REMAINING="$REPORT_DIR/ruff-kalan-$STAMP.txt"
"${RUFF[@]}" check "${TARGETS[@]}" --output-format concise --exit-zero > "$REMAINING" 2>&1 || true
LEFT="$(grep -cE '^[^ ].+:[0-9]+:[0-9]+:' "$REMAINING" || true)"
say "Kalan ruff bulgusu: $LEFT (liste: ${REMAINING#"$ROOT"/})"

# ── 6. isteğe bağlı: Aider ───────────────────────────────────────────────
if (( USE_AIDER )) && (( LEFT > 0 )); then
  head_ "Aider ile kalan bulgular ($AIDER_MODEL)"
  command -v aider >/dev/null 2>&1 || die "aider bulunamadı (kurulum: pipx install aider-chat)."
  export OLLAMA_API_BASE="${OLLAMA_API_BASE:-http://127.0.0.1:11434}"

  # En çok bulgusu olan, küçük dosyalar önce.
  mapfile -t FILES < <(grep -oE '^[^ :]+\.py' "$REMAINING" | sort | uniq -c | sort -rn | awk '{print $2}')
  done_count=0
  for f in "${FILES[@]}"; do
    (( done_count >= AIDER_MAX_FILES )) && break
    [[ -f "$f" ]] || continue
    size=$(stat -c %s "$f")
    if (( size > AIDER_MAX_BYTES )); then
      say "atlandı (çok büyük, $size bayt): $f"
      continue
    fi
    done_count=$((done_count + 1))
    findings="$(grep -F "$f:" "$REMAINING" | head -n 30)"
    before_sha="$(git rev-parse HEAD)"
    say "→ $f"
    timeout 900 aider --model "$AIDER_MODEL" --yes-always --no-show-model-warnings \
      --no-check-update --no-pretty --auto-commits \
      --message "Aşağıdaki ruff bulgularını bu dosyada düzelt. Davranışı değiştirme, yalnızca bulguları gider:
$findings" "$f" >> "$LOG" 2>&1 || say "  aider hata/zaman aşımı ile bitti"

    # Aider commit etmediyse kalan değişiklikleri bırakma.
    git checkout -q -- . 2>/dev/null || true
    if [[ "$(git rev-parse HEAD)" == "$before_sha" ]]; then
      say "  değişiklik yok"
      continue
    fi
    now_fail="$(count_failures)"
    if (( now_fail > BASE_FAIL )); then
      git reset -q --hard "$before_sha"      # yalnızca bu dalda, az önce aider'ın yaptığı commit
      say "  ⚠️ testler kötüleşti ($BASE_FAIL → $now_fail) → bu dosyanın değişikliği geri alındı"
    else
      BASE_FAIL="$now_fail"
      say "  ✅ commit edildi (başarısız test: $now_fail)"
    fi
  done
  "${RUFF[@]}" check "${TARGETS[@]}" --output-format concise --exit-zero > "$REMAINING" 2>&1 || true
  LEFT="$(grep -cE '^[^ ].+:[0-9]+:[0-9]+:' "$REMAINING" || true)"
fi

# ── 7. özet ──────────────────────────────────────────────────────────────
head_ "Özet"
say "Dal: $BRANCH"
say "ruff bulgusu: $BASE_RUFF → $LEFT"
say "Başarısız test (son): $BASE_FAIL"
say "Commit'ler:"
git log --oneline "$START_BRANCH..$BRANCH" | tee -a "$LOG" || true
say ""
say "İncelemek için : git diff $START_BRANCH..$BRANCH"
say "Kabul etmek için: git checkout $START_BRANCH && git merge $BRANCH"
say "Vazgeçmek için : git checkout $START_BRANCH && git branch -D $BRANCH"
say "Tam rapor      : ${LOG#"$ROOT"/}"
