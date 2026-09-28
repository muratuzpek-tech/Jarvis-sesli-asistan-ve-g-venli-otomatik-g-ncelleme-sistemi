#!/usr/bin/env bash
# wayland_kurulum.sh — JARVIS'in Ubuntu (GNOME/Wayland) üzerinde klavye/fare
# kontrolü ve harici monitör parlaklığı için gereken tek seferlik kurulum.
#
# Yaptıkları (her adım ekrana yazılır, tekrar çalıştırmak güvenlidir):
#   1. ydotool (Wayland'de klavye/fare) ve ddcutil (harici monitör parlaklığı) kurar.
#   2. uinput ve i2c-dev çekirdek modüllerini şimdi yükler ve açılışta yüklenecek şekilde ayarlar.
#   3. /dev/uinput için DAR bir izin verir: yalnızca "uinput" adlı yeni bir gruba
#      yazma izni. (Kullanıcı "input" grubuna EKLENMEZ — o grup tüm klavye
#      girdilerini OKUYABİLİR, bu gereksiz bir yetki olurdu.)
#   4. ydotoold'u kullanıcı servisi olarak açılışta başlatır (soket yalnızca sana ait, 0600).
#   5. Harici monitör için i2c erişimini ayarlar ve ddcutil ile test eder.
#   6. Sonunda gerçek bir test yapar: fareyi 1 piksel oynatıp geri alır.
#
# Kullanım:
#   bash scripts/wayland_kurulum.sh            # kur
#   bash scripts/wayland_kurulum.sh --geri-al  # yaptığı her şeyi geri al
set -euo pipefail

RULE_UINPUT=/etc/udev/rules.d/60-jarvis-uinput.rules
MODULES_CONF=/etc/modules-load.d/jarvis.conf
SERVICE_NAME=jarvis-ydotoold.service
SERVICE_FILE="$HOME/.config/systemd/user/$SERVICE_NAME"
ENV_FILE="$HOME/.config/environment.d/jarvis-ydotool.conf"
USER_NAME="$(id -un)"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '  ❌ %s\n' "$*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || die "Bu script'i sudo ile DEĞİL, kendi kullanıcınla çalıştır (gerektiğinde sudo'yu kendisi ister)."
command -v systemctl >/dev/null || die "systemd bulunamadı."

# ── geri alma ────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--geri-al" ]]; then
  step "Geri alınıyor"
  systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$ENV_FILE"
  systemctl --user daemon-reload
  sudo rm -f "$RULE_UINPUT" "$MODULES_CONF"
  sudo gpasswd -d "$USER_NAME" uinput 2>/dev/null || true
  sudo gpasswd -d "$USER_NAME" i2c 2>/dev/null || true
  sudo setfacl -x "u:$USER_NAME" /dev/uinput 2>/dev/null || true
  sudo udevadm control --reload
  ok "Servis, izin kuralları ve grup üyelikleri kaldırıldı."
  warn "Paketler (ydotool, ddcutil) kaldırılmadı. İstersen: sudo apt remove ydotool ddcutil"
  exit 0
fi
[[ -z "${1:-}" ]] || die "Bilinmeyen seçenek: $1 (yalnızca --geri-al)"

# ── 1. paketler ──────────────────────────────────────────────────────────
step "1/6 Paketler: ydotool, ddcutil, acl"
sudo apt-get install -y ydotool ddcutil acl
command -v ydotool  >/dev/null || die "ydotool kurulamadı."
command -v ydotoold >/dev/null || die "ydotoold bulunamadı (paket eski bir ydotool sürümü olabilir)."
ok "$(ydotool --help 2>&1 | head -n1 || echo ydotool kurulu)"

# ── 2. çekirdek modülleri ────────────────────────────────────────────────
step "2/6 Çekirdek modülleri: uinput, i2c-dev"
printf 'uinput\ni2c-dev\n' | sudo tee "$MODULES_CONF" >/dev/null
sudo modprobe uinput
sudo modprobe i2c-dev
ok "Yüklendi ve açılışta otomatik yüklenecek ($MODULES_CONF)."

# ── 3. /dev/uinput izni ──────────────────────────────────────────────────
step "3/6 /dev/uinput izni (yalnızca 'uinput' grubu)"
getent group uinput >/dev/null || sudo groupadd --system uinput
sudo usermod -aG uinput "$USER_NAME"
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="uinput", MODE="0660", OPTIONS+="static_node=uinput"' \
  | sudo tee "$RULE_UINPUT" >/dev/null
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput
# Grup üyeliği ancak oturum yeniden açılınca etkinleşir; beklemeden çalışsın
# diye bu açılış için geçici bir ACL de verilir (yeniden başlatınca silinir,
# sonrasında kalıcı udev kuralı devralır).
sudo setfacl -m "u:$USER_NAME:rw" /dev/uinput
ls -l /dev/uinput
ok "Kalıcı kural: $RULE_UINPUT"

# ── 4. ydotoold kullanıcı servisi ────────────────────────────────────────
step "4/6 ydotoold kullanıcı servisi"
mkdir -p "$(dirname "$SERVICE_FILE")" "$(dirname "$ENV_FILE")"
cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=ydotool daemon for JARVIS (Wayland keyboard/mouse)
After=graphical-session.target

[Service]
ExecStart=/usr/bin/ydotoold --socket-path=%t/.ydotool_socket --socket-perm=0600
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
# ydotool istemcisinin soketi bulması için (yeni oturumlardan itibaren geçerli).
echo 'YDOTOOL_SOCKET=${XDG_RUNTIME_DIR}/.ydotool_socket' > "$ENV_FILE"
systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"
sleep 1
if systemctl --user is-active --quiet "$SERVICE_NAME"; then
  ok "Servis çalışıyor ve açılışta başlayacak."
else
  systemctl --user status "$SERVICE_NAME" --no-pager | tail -n 15 || true
  die "ydotoold başlatılamadı (yukarıdaki log)."
fi

# ── 5. harici monitör (ddcutil) ──────────────────────────────────────────
step "5/6 Harici monitör parlaklığı (ddcutil)"
getent group i2c >/dev/null || sudo groupadd --system i2c
sudo usermod -aG i2c "$USER_NAME"
for dev in /dev/i2c-*; do
  [[ -e "$dev" ]] && sudo setfacl -m "u:$USER_NAME:rw" "$dev"
done
DDC_OUT="$(timeout 60 ddcutil detect --brief 2>&1 || true)"
printf '%s\n' "$DDC_OUT" | sed 's/^/  /'
if grep -q "^Display" <<<"$DDC_OUT"; then
  ok "Monitör DDC/CI ile algılandı."
  timeout 30 ddcutil getvcp 10 2>&1 | sed 's/^/  /' || warn "Parlaklık okunamadı."
else
  warn "Monitör DDC/CI ile algılanmadı. Monitör menüsünden 'DDC/CI' ayarının AÇIK olduğundan emin ol."
  warn "NVIDIA sürücüsünde bazen ek ayar gerekir; sonucu bana gönder."
fi

# ── 6. canlı test ────────────────────────────────────────────────────────
step "6/6 Canlı test: fare 1 piksel sağa ve geri"
export YDOTOOL_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/.ydotool_socket"
if ydotool mousemove -x 1 -y 0 && ydotool mousemove -x -1 -y 0; then
  ok "ydotool çalışıyor — JARVIS artık Wayland'de klavye/fare kullanabilir."
else
  warn "ydotool komutu hata verdi. Oturumu kapatıp açtıktan sonra tekrar dene."
fi

step "Bitti"
echo "  • Grup üyelikleri (uinput, i2c) bir sonraki oturum açışında kalıcı olarak etkinleşir."
echo "  • Kontrol için: .venv/bin/python scripts/wayland_teshis.py"
echo "  • Geri almak için: bash scripts/wayland_kurulum.sh --geri-al"
