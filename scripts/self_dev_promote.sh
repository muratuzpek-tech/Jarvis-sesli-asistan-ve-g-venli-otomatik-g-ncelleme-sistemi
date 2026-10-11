#!/usr/bin/env bash
# Jarvis self-dev terfisi: sandbox'taki jarvis/self-dev dalinin ana depo
# main'inde OLMAYAN commit'lerini `git format-patch` ile ~/.jarvis-self/out/
# altina yazar ve nasil uygulanacagini yazdirir. Ana depoyu DEGISTIRMEZ,
# hicbir seyi otomatik uygulamaz (ana depo yalnizca okunur: main'in commit'leri
# sandbox'a getirilir, karsilastirma sandbox'ta yapilir).
#
# Ortam: JARVIS_SANDBOX (~/jarvis-sandbox), JARVIS_REPO (bu betigin deposu),
#        JARVIS_SELF_DIR (~/.jarvis-self)
set -euo pipefail

SANDBOX="${JARVIS_SANDBOX:-$HOME/jarvis-sandbox}"
REPO="${JARVIS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SELF_DIR="${JARVIS_SELF_DIR:-$HOME/.jarvis-self}"
BRANCH="jarvis/self-dev"
BASE_REF="refs/self-dev/main"

if [ ! -d "$SANDBOX/.git" ]; then
    echo "HATA: sandbox yok: $SANDBOX (önce: python scripts/self_dev.py init)" >&2
    exit 1
fi
if [ -n "$(git -C "$SANDBOX" remote)" ]; then
    echo "HATA: sandbox'ta git uzağı var; güvenlik için durduruldu" >&2
    exit 1
fi
git -C "$SANDBOX" rev-parse --verify -q "$BRANCH" >/dev/null || {
    echo "HATA: sandbox'ta $BRANCH dalı yok" >&2
    exit 1
}

# Ana deponun main'i sandbox'a (yalnizca sandbox'ta bir ref olarak) getirilir.
git -C "$SANDBOX" fetch -q --no-tags "$REPO" "+main:$BASE_REF"

if [ "$(git -C "$SANDBOX" rev-list --count "$BASE_REF..$BRANCH")" = "0" ]; then
    echo "Terfi edilecek commit yok: $BRANCH, ana depo main'inde olmayan commit içermiyor."
    exit 0
fi

OUT="$SELF_DIR/out/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
# --ignore-if-in-upstream: main'e zaten uygulanmis (git am ile) yamalar atlanir
git -C "$SANDBOX" format-patch -q --ignore-if-in-upstream -o "$OUT" "$BASE_REF..$BRANCH"

mapfile -t PATCHES < <(find "$OUT" -maxdepth 1 -name '*.patch' | sort)
if [ "${#PATCHES[@]}" -eq 0 ]; then
    rmdir "$OUT" 2>/dev/null || true
    echo "Terfi edilecek yama yok: tüm commit'ler ana depoda zaten var."
    exit 0
fi

echo "${#PATCHES[@]} yama yazıldı: $OUT"
for p in "${PATCHES[@]}"; do
    echo "  $p"
done
echo
echo "Uygulamak için (Jarvis KAPALIYKEN, önce yamaları gözden geçirin):"
echo "  cd \"$REPO\""
printf '  git am --3way'
for p in "${PATCHES[@]}"; do
    printf ' "%s"' "$p"
done
echo
echo "Ana depo bu betik tarafından değiştirilmedi."
