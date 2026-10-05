# Tek Güvenlik Kapısı (`security_gate.py`) — Envanter ve Geçiş Planı

Tarih: 2026-10-05 · Durum: taslak, kod değişikliği yok.

Satır numaraları, bu belge yazıldığı andaki HEAD'e aittir. 1, 2, 4 ve 5.
bulguların düzeltmeleri ve `discovered_*` için fail-closed onay bu HEAD'de
mevcuttur. Kod değiştikçe satırlar kayabilir; fonksiyon adları esas alınmalı.

---

## 0. Özet

Bugün bir aracın "tehlikeli mi, onay gerekir mi" kararı **en az 12 ayrı yerde**
veriliyor. Onay **5 ayrı depoda** tutuluyor:

1. `main.py` tek bekleyen-işlem yuvası
2. `approval_service`
3. Araç içi kod sözlükleri: file_controller, code_helper, self_improve, dev_agent
4. `agent_loop` görev dosyası
5. CLI konsol `input()`

"Gerçek kullanıcı turu" da **3 ayrı şekilde** tanımlı:

- `main._grant_dangerous_confirmation`
- `approval_service.mark_user_turn`
- `dev_agent.note_user_turn`

Bugünkü hatalar hep bu dağınıklıktan çıktı. İki örnek: risk ile yürütmenin aynı
adımı farklı çözümlemesi ve aynı eylemin bir yolda HIGH, diğerinde onaysız olması.

Envanter sırasında görülen ve **hâlâ açık** olan yollar (ayrıntı §2):

| # | Yol | Neden önemli |
|---|-----|--------------|
| A | Sesli araç yolunda onaysız çalışan değiştirici araçlar: `file_controller` (move/delete_all dışı), `browser_control`, `desktop_control`, `reminder`, `save_memory`, `file_processor`, `youtube_video save`, `system_scan_and_repair`, `task_manager` | Aynı eylem agent_loop/Brain Team'de onay istiyor (3. bulgu) |
| B | `start_parallel_task` onay kodunu modele gösteriyor. Kodun açılması için **herhangi bir** sonraki kullanıcı turu yetiyor (`dev_agent.py:63-72`) | 4. bulgunun aynısı, terminal dışında |
| C | CLI ajanı: model `file_write(self_repair=true)` gönderirse `core/agent.py` onaysız yeniden yazılıyor (`core/agent.py:68`, `:206-214`) | Bir sonraki açılışta kod çalıştırma |
| D | CLI eklentileri: `src/jarvis/plugins/*.py` başlangıçta `exec_module` ile yükleniyor (`plugin_manager.py:36`); araçları onaysız çağrılıyor (`core/agent.py:328`) | Dosya sistemine yazabilen her şey kod çalıştırır |
| E | Zamanlayıcı: modelin kaydettiği metin daha sonra "gerekirse ilgili aracı da çağır" istemiyle modele geri veriliyor (`main.py:3258-3277`) | Kullanıcı yokken A'daki araçlar çalışabilir |
| F | `coder_ai` onaydan sonra **herhangi bir yola** yazıyor; birleşik yazma politikası yok (`coder_ai.py:83`). `analyze` her yolu onaysız okuyup LLM'e veriyor | Onay metni hedefi göstermeyebilir (12. bulgu) |
| G | `security_ai.classify_risk` tanımadığı her şeye `low` diyor (`security_ai.py:135`) | Yeni executor eylemi sessizce onaysız |
| H | Dashboard metni Gemini'ye doğrudan gidiyor. `mark_user_turn` ve onay işleme yok (`main.py:3340-3356`) | Güvenli tarafta ama tutarsız: uzaktan "evet" hiçbir şeyi onaylamıyor, telefon sesi ise onaylıyor |

---

## 1. Giriş yolları

Her satır, bir aracın **çalıştırılmaya başlandığı** noktayı gösterir. "Tetikleyen",
çağrıyı kimin başlattığıdır.

### 1.1 Modeli tetikleyen kanallar

Bunlar araç çağırmaz; Gemini'ye bir tur başlatır. Model o turda E1/E2 üzerinden
araç çağırabilir.

| Kanal | Dosya:satır | Gerçek kullanıcı turu sayılır mı |
|-------|-------------|----------------------------------|
| Mikrofon (canlı ses, transkripsiyon) | `main.py:2760-2790` | Evet: `note_user_turn` + `approval_service.mark_user_turn` (`:2770`), sesli onay `_apply_spoken_confirmation` (`:2778`) |
| Arayüz metin kutusu | `ui.py:2836`, `:2911` → `main.py:1572` `_on_ui_text_command` | Evet (`:1577-1578`) |
| Dashboard `/api/command` | `dashboard/server.py:752` → `main.py:3340` | **Hayır**; metin doğrudan `send_client_content` |
| Dashboard `/ws/phone-audio` | `dashboard/server.py:791` | Evet; ses, mikrofon yolundaki transkripsiyona girer |
| Zamanlayıcı (task_manager) | `main.py:3258` (`pop_due_tasks`) | Hayır; sentetik istem |
| Proaktif mod / sabah brifingi | `main.py:3280` | Hayır |
| Arka plan sonucu, Brain/agent bildirimleri (`self.speak("[...]")`) | `main.py:2061` ve 21 çağrı yeri | Hayır; araç çıktısı, model için **güvenilmeyen girdi** |

### 1.2 Aracı çalıştıran yollar

| ID | Yol | Giriş noktası (dosya:satır) | Tetikleyen |
|----|-----|------------------------------|-----------|
| E1 | Gemini Live araç çağrısı (eski dağıtım) | `main.py:2157` `_execute_tool`; dallar `:2202-2500` | Model |
| E2 | Jarvis 2.0 registry | `main.py:2164` → `_execute_registry_tool` `:1377` → `tools/registry.py:172`. Ayrıca `code_helper`/`dev_agent` yönlendirmesi `main.py:2374`, `:2390` | Model |
| E3 | ReAct alt ajanı | `tools/agent/react_runtime.py:294` → kayıtlı **her** registry aracı, ada göre | İç LLM (Gemini/Ollama) |
| E4 | Deterministik metin yönlendiricileri (`_on_text_command`, `main.py:1581`) | yerel dosya bilgisi `:1649`; Brain sağlık `:1679`; Brain başlat `:1706`; Brain durum `:1724`; genel görev → agent_loop `:1739`, `:1978`, `:1997`; terminal yönlendirici `:1757-1777`; onaylı terminal `:1612`; SYSTEM_READ → windows_system `:1790`; FILE_MODIFICATION → Brain `:1836-1851`; FILE_ANALYSIS → Brain `:1919-1925` | Kullanıcı metni (model yok) |
| E5 | agent_loop arka plan döngüsü | `agent_loop.py:683` `_tick` → `_process_task` → `tools_kopru.call_tool` `:263`. Onaylı yürütme: `approve_task` `agent_loop.py:238` ← `main.py:1308`. Keşif: `agent_loop.py:621` (env ile açılır) | İç LLM planlayıcı (`agent_loop.py:154`) |
| E6 | Brain Team orkestratörü | `brain_orchestrator.py:1150` `_tick` → `_risk_of_step` `:731` → `_execute_step` `:813` → bus → `executor_ai._ALLOWED_ACTIONS` (`brains/executor_ai.py:107`), `coder_ai` (`brains/coder_ai.py:83`), research_ai. Onaylı yürütme: `approve` `:380` ← `main.py:1190` | İç LLM planlayıcı + E4 |
| E7 | terminal_tool | `actions/terminal_tool.py:131`; çağıranlar `main.py:2364` (E1), `:1759` (E4), `:1612` (onaylı E4) | Model / kullanıcı metni |
| E8 | code_helper (registry yoksa) | `main.py:2374` → `actions/code_helper.py:849` | Model |
| E9 | dev_agent / start_parallel_task | `main.py:2280` → `agent_board.py:131` → `dev_agent`; `main.py:2390` (registry yoksa) | Model |
| E10 | self_improve | `main.py:2404`; CLI `cli.py:96` | Model / CLI kullanıcısı |
| E11 | Entegrasyon (dış kod) | `tools_kopru` `entegrasyon_uygula` → `github_arama.py:335` → `entegrasyon.py:335`. Keşif: `github_arama.py:194`, `discovery.py:754` | E5 onayı |
| E12 | Dashboard dosya yükleme | `dashboard/server.py:842` `/api/upload` (uploads klasörüne yazar) | Kimliği doğrulanmış uzak kullanıcı |
| E13 | Zamanlayıcı kaydı | `main.py:2272` → `actions/automation.py:150`; tetikleme §1.1 | Model |
| E14 | Hatırlatıcı (OS zamanlayıcısı) | `main.py:2292` → `actions/reminder.py:287`. Python betiği üretip OS'e kaydeder; betik Jarvis **dışında** sonradan çalışır | Model |
| E15 | CLI metin ajanı | `cli.py:106` → `core/agent.py:294`; `TOOLS` `:257` | İç LLM |
| E16 | CLI eklentileri | `core/plugin_manager.py:36` (yükleme = kod çalıştırma), `core/agent.py:328` (çağrı) | İç LLM; diske yazabilen herkes |

---

## 2. Her yol için: karar, onay, modele kod

| ID | "Tehlikeli mi" kararını veren | Onayın alındığı yer | Modele kod gösteriliyor mu |
|----|-------------------------------|---------------------|----------------------------|
| E1 | `tool_gate.needs_confirmation` (`tool_gate.py:37`; tablolar `:20-31`, `discovered_` öneki) — **yalnızca** send_message, computer_control, open_app, shutdown_jarvis, self_improve, agentic_code, discovered_*. Araç içi kurallar: `file_controller` (yalnızca move/delete_all, `file_controller.py:793`, `:844`), `code_helper` (yalnızca var olan dosyanın üzerine yazma, `code_helper.py:214`), `computer_settings._DANGEROUS_ACTIONS` (`computer_settings.py:586`) + `main.py:2336-2363`, `game_updater` shutdown (`main.py:2452`, `game_updater.py:958`), `shutdown_jarvis` (`main.py:2487`). Diğer tüm E1 araçları: **karar yok, onaysız** | `main.py` yuvası: `_set_pending_dangerous` `:1036`, `_grant_dangerous_confirmation` `:1021`, `_consume_dangerous_confirmation` `:1053`, parmak izi `_action_fingerprint` `:1044`. Kod-saklama yolu: `_CONFIRM_CODE_TOOLS` `:1078`, `_prepare_confirmed_args` `:1118`, `_redact_confirmation_result` `:1131`. Gate yolu: `main.py:2186` | Hayır (4/5 düzeltmesinden sonra). `start_parallel_task`: **evet** (B) |
| E2 | `tools/security.py:80` `SecurityManager.check`: seviye (`READ_ONLY`/`NORMAL`/`DANGEROUS`/`DESTRUCTIVE`) aracın kaydında sabit; `_sanitize_inputs` regex listesi | `main.py:1397` `ctx.dangerous_confirmed = _consume_dangerous_confirmation(...)`; bekleyen işlem `:1437` | Hayır |
| E3 | E2 ile aynı (`registry.execute`); `ctx` dış `react_agent` çağrısından devralınıyor | Yok; iç turda kullanıcı turu olamaz, DANGEROUS+ araçlar `CONFIRMATION_REQUIRED` ile düşer | — |
| E4 | Yönlendiricinin kendisi (eşleşme = karar). Terminal için `terminal_tool._is_readonly` (`terminal_tool.py:60`); windows_system için `windows_shell._ALLOWED_COMMANDS` (`windows_shell.py:60`); dosya değişikliği Brain'e (E6) devrediliyor | Terminal: `_pending_terminal_command` + `approval_service` (`main.py:1612`, `:1757-1777`) | Hayır |
| E5 | `tools_kopru.is_destructive` (`tools_kopru.py:233`; `_READONLY_TOOLS` `:208`, `_SAFE_SETTINGS_ACTIONS` `:226`, file_controller için `READONLY_ACTIONS`); çalışma anında `call_tool` `:263` + `_APPROVED_TOOL` contextvar | `agent_loop.set_approval_hook` (`:368`) → `main.request_agent_loop_approval` `:1254` / `_handle_agent_loop_reply` `:1308` → `approve_task(expected_action)` `agent_loop.py:238` | Hayır |
| E6 | `_risk_of_step` (`brain_orchestrator.py:731`) → `_resolve_step_call` `:724` → `_resolve_action_with_file_modification` `:529` → `security_ai.classify_risk` (`security_ai.py:121`; tablolar `:25-50`, **varsayılan low**) + `_HIGH_RISK_KEYWORDS` (`brain_orchestrator.py:115`, yalnızca yükseltir). Uygulama allowlist'i: `executor_ai._ALLOWED_ACTIONS` | `_request_approval` `:304` → `main.request_brain_team_approval` `:1169` / `_handle_brain_team_reply` `:1190` | Hayır |
| E7 | `terminal_tool._is_readonly` (`:60`) | `approval_service.consume` (`core/approval_service.py:50`) + `main` yuvası (E1) veya `_pending_terminal_command` (E4) | Hayır (4. düzeltmeden sonra) |
| E8 | `code_helper`'ın kendisi: yalnızca edit/optimize üzerine yazma (`:214`); `run` `:676`, `build` `:522` ve `write` için karar yok. `_try_run_simple` AST yasak listesi `:395` | E1'deki kod-saklama yolu | Hayır |
| E9 | `dev_agent.confirmation_problem` (`dev_agent.py:75`), `note_user_turn` (`:63`) | Araç içi kod sözlüğü `_pending_dev_agent`; "kullanıcı turu oldu mu" kontrolü yalnızca zamana bakıyor | **Evet** (B) |
| E10 | `self_improve._is_allowed_target` (`self_improve.py:89`), `PROTECTED_FILES` (`:69`) | `_pending_self_improve` (`:381`) + E1 kod-saklama; CLI'de doğrudan `cli.py:96` | Hayır (E1); CLI'de kullanıcı kendisi |
| E11 | E5 (`entegrasyon_uygula` salt-okunur değil); `entegrasyon._verify_module` (`:228`) statik | E5 onayı | Hayır |
| E12 | `dashboard/server.py` `_auth` + `_safe_filename` | Kimlik doğrulama (oturum anahtarı) | — |
| E13 | Yok. Kayıt onaysız; tetiklenince E1 kuralları geçerli ama kullanıcı turu yok | Yok | Hayır |
| E14 | Yok | Yok | Hayır |
| E15 | `core/agent.py` araç başına `_ask_confirmation` (`:171`, konsol `input`); `_safe_path` (`:40`, yalnızca $HOME). **İstisna:** `self_repair=true` onaysız (C) | Konsol | Hayır |
| E16 | Yok | Yok | Hayır |

---

## 3. Araç listesi

Sınıflar:
- **RO**: salt okuma
- **MUT**: yerel değişiklik (dosya/ayar/kalıcı durum)
- **EXT**: dışarıya etki (mesaj, tarayıcıda tıklama/form)
- **EXEC**: kod/program çalıştırma
- **SYS**: sistem gücü/oturum

"Bugün onay" sütunu: ✅ her zaman, ◐ eylem/yola göre, ✗ yok.

### 3.1 Gemini araçları (`main.py:286` `TOOL_DECLARATIONS`)

| Araç | Kaynak | Sınıf | Yollar | Bugün onay |
|------|--------|-------|--------|------------|
| open_app | `actions/open_app.py` | EXEC | E1 | ✅ tool_gate |
| web_search | `actions/web_search.py` | RO | E1, E5, E15 | ✗ (gerek yok) |
| terminal | `actions/terminal_tool.py` | RO / EXEC (komuta göre) | E1, E4 | ◐ `_is_readonly` |
| system_status | `actions/system_monitor.py` | RO | E1, E5, E15 | ✗ |
| system_scan_and_repair | `actions/system_scan.py` | EXEC (tüm modülleri import eder; env ile pip install) | E1, E5 | E5 ✅, **E1 ✗** |
| weather_report | `actions/weather_report.py` | RO | E1, E5 | ✗ |
| send_message | `actions/send_message.py` | EXT | E1, E5 (E6'da engelli) | ✅ |
| reminder | `actions/reminder.py` | MUT (OS görevi + betik) | E1, E5 | E5 ✅, **E1 ✗** |
| youtube_video | `actions/youtube_video.py` | EXT + MUT (`save`) | E1 | ✗ |
| screen_process | `actions/screen_processor.py` | RO (kamera/ekran → LLM; mahremiyet) | E1 | ✗ |
| close_camera | `main.py:2332` | MUT (önemsiz) | E1 | ✗ |
| computer_settings | `actions/computer_settings.py` | MUT / SYS | E1, E5 | ◐ yalnızca restart/shutdown/lock (E1); E5 ses/parlaklık dışı her şey |
| browser_control | `actions/browser_control.py` | EXT (click/type/fill_form) | E1 | ✗ |
| file_controller | `actions/file_controller.py` | RO / MUT (eyleme göre) | E1, E5, E6, E4 | E5/E6 ✅ fail-closed; **E1 yalnızca move/delete_all** |
| task_manager | `actions/automation.py` | MUT (ertelenmiş istem) | E1 | ✗ |
| health_check | `actions/health_check.py` | RO (geçici test dosyası yazar/siler) | E1 | ✗ |
| start_parallel_task | `actions/agent_board.py` | EXEC | E1 | ✅ ama kod modelde (B) |
| check_agent_board | `actions/agent_board.py` | RO | E1 | ✗ |
| desktop_control | `actions/desktop.py` | MUT (organize/clean/wallpaper); LLM kod çalıştırma kapalı (`desktop.py:82`) | E1 | ✗ |
| code_helper | `actions/code_helper.py` / registry'de agentic_code'a yönlenir | EXEC + MUT | E1 → E2 / E8 | E2 ✅; **E8 yalnızca üzerine yazma** |
| code_search | `main.py:2202` (agentgrep alt süreci) | RO | E1 | ✗ |
| dev_agent | `actions/dev_agent.py` / agentic_code | EXEC | E1 → E2 / E9 | ✅ (E9'da kod modelde) |
| self_improve | `actions/self_improve.py` | MUT (Jarvis kodu) | E1, CLI | ✅ |
| agent_loop | `actions/agent_loop.py` | MUT (görev ekler); approve kaldırıldı | E1, E4 | ✗ (ekleme), adımlar E5'te |
| recall_conversation | `actions/conversation_log.py` | RO | E1 | ✗ |
| github_arama | `actions/github_arama.py` | RO | E1, E5 | ✗ |
| discovered_topydo | `actions/discovered_topydo.py` | MUT (dış kod) | E1, E5 | ✅ fail-closed |
| discovered_jc | `actions/discovered_jc.py` | EXEC (dış kod) | E1, E5 | ✅ fail-closed |
| computer_control | `actions/computer_control.py` | EXT / EXEC (klavye/fare) | E1 | ✅ |
| game_updater | `actions/game_updater.py` | EXEC (Steam başlatır) + SYS (shutdown) | E1 | ◐ yalnızca shutdown |
| flight_finder | `actions/flight_finder.py` | RO + MUT (`save`) | E1 | ✗ |
| shutdown_jarvis | `main.py:2487` | SYS | E1 | ✅ |
| file_processor | `actions/file_processor.py` | MUT (çıktı dosyası yazar) | E1 | ✗ |
| save_memory | `main.py:2220` | MUT (kalıcı bellek → sistem istemine girer) | E1 | ✗ |

### 3.2 Registry araçları (`tools/`)

| Araç | Kaynak | Sınıf | Yollar | Bugün onay |
|------|--------|-------|--------|------------|
| agentic_code | `tools/developer/__init__.py:30` | EXEC + MUT ($HOME içinde herhangi bir klasör) | E2, E3 | ✅ DESTRUCTIVE |
| react_agent | `tools/agent/__init__.py:25` | Yönlendirici (her registry aracı) | E2 | ✗ (iç çağrılar kendi seviyesiyle) |
| spotify_control | `tools/media/__init__.py:27` | EXT (önemsiz) | E2, E3 | ✗ |

### 3.3 agent_loop köprüsü (`tools_kopru.ALLOWED_TOOLS`, `tools_kopru.py:152`)

Yukarıdakilere ek olarak:

| Araç | Sınıf | Bugün onay (E5) |
|------|-------|-----------------|
| windows_system | RO (sabit allowlist) | ✗ |
| github_arac_bul_ve_degerlendir | MUT (karantinaya indirir) | ✗ (bilinçli) |
| entegrasyon_uygula | EXEC (Jarvis'e dış kod yazar) | ✅ |
| discovery_register | MUT (kayıt) | ✅ |

### 3.4 Brain Team uygulayıcıları

| Araç | Kaynak | Sınıf | Bugün onay (E6) |
|------|--------|-------|-----------------|
| file_controller | `executor_ai.py:107` | RO / MUT | ✅ fail-closed |
| backup_create | `executor_ai.py:107` | MUT | ✗ (MEDIUM onay istemiyor) |
| backup_rollback | `executor_ai.py:107` | MUT (geniş) | ✅ |
| vault_encrypt / vault_decrypt | `executor_ai.py:107` | MUT | ✅ |
| github_search | `executor_ai.py:107` | RO | ✗ |
| windows_system | `executor_ai.py:107` | RO | ✗ |
| coder_ai modify_critical_file | `coder_ai.py:83` | MUT (herhangi bir yol, F) | ✅ |
| coder_ai analyze | `coder_ai.py:76` | RO (herhangi bir yol → LLM, F) | ✗ |
| research_ai | `brains/research_ai.py` | RO (ağ) | ✗ |

### 3.5 CLI ajanı (`core/agent.py:257`)

| Araç | Sınıf | Bugün onay (E15) |
|------|-------|------------------|
| web_search, system_status, file_list | RO | ✗ |
| file_read | RO | ✅ konsol |
| file_write | MUT | ✅ konsol; **`self_repair=true` ile ✗** (C) |
| run_python | EXEC | ✅ konsol |
| remember | MUT | ✗ |
| file_delete | MUT | ✅ konsol |
| eklenti araçları | ? | ✗ (D) |

---

## 4. Öneri: `authorize(tool, args, source) -> Decision`

### 4.1 Tasarım ilkeleri (bugünkü hatalardan çıkan)

1. **Tek çözümleme.** `authorize` aracı ve argümanları **bir kez** çözer (takma ad,
   eylem, hedef yol) ve bir `ResolvedCall` döndürür. Yürütme yalnızca bu nesneyle
   yapılır, yeniden çözümleme yoktur. Bu, `_risk_of_step` ile `_execute_step`
   arasındaki hatanın genel çözümüdür. LLM ile çözümleme (ör.
   `computer_settings._detect_action`, Brain planlayıcısı) `authorize`'dan
   **önce** yapılmalı, sonra asla.
2. **Fail-closed.** Kayıtsız araç → `DENY`. Kayıtlı ama sınıfı/eylemi bilinmeyen
   → `NEEDS_APPROVAL`. Okuma dışındaki her şeyin varsayılanı onaydır.
3. **Onay modele hiç görünmez.** Kapı kod üretmez. Onay kimliği iç bir anahtardır.
   Modele giden metin `Decision.model_message`'dır ve sır içermez.
4. **Onayı yalnızca girdi katmanı verebilir.** `UserTurn` bir yetenek nesnesidir.
   Yalnızca arayüz metni, son sesli transkript, CLI konsolu ve (karara bağlı)
   kimliği doğrulanmış dashboard üretebilir. Model argümanları ve araç çıktıları
   `UserTurn` üretemez.
5. **Çoklu bekleyen onay.** Tek yuva yerine, her biri kendi parmak iziyle tutulan
   kayıtlar olmalı. Parmak izi: araç + normalize argümanlar + çözülmüş hedefler +
   kaynak + görev kimliği. "Evet" yalnızca **en son duyurulan** isteğe gider.
   9. bulgudaki karışma böylece biter.
6. **Merkezi yol politikası.** Okuma kökleri, yazma kökleri, gizli bileşen yasağı,
   symlink kontrolü ve korumalı dosyalar tek modülde toplanır.
7. **Tek denetim kaydı.** Her karar ve her onay sonucu tek audit kaydına gider;
   yol `JARVIS_HOME`'a bağlıdır. Bugün `tool_gate.audit_entry` `~/.jarvis`,
   `core/audit_log` ise `memory_dir` kullanıyor.

### 4.2 Arayüz

```python
# src/jarvis/security_gate.py
from dataclasses import dataclass
from enum import Enum

class Source(Enum):
    MODEL_LIVE = "model_live"        # E1, E2
    REACT = "react"                  # E3
    ROUTER = "router"                # E4 (kullanıcı metninden deterministik)
    AGENT_LOOP = "agent_loop"        # E5
    BRAIN_TEAM = "brain_team"        # E6
    SCHEDULER = "scheduler"          # E13 tetiklemesi
    PROACTIVE = "proactive"
    CLI_AGENT = "cli_agent"          # E15
    PLUGIN = "plugin"                # E16

class Effect(Enum):
    READ = 0; MUTATE = 1; EXTERNAL = 2; EXECUTE = 3; SYSTEM = 4

class Verdict(Enum):
    ALLOW = "allow"; NEEDS_APPROVAL = "needs_approval"; DENY = "deny"

@dataclass(frozen=True)
class ResolvedCall:
    tool: str
    action: str | None
    params: dict              # normalize edilmiş, yürütmeye AYNEN gidecek
    targets: tuple[str, ...]  # çözülmüş mutlak yollar / alıcılar / URL'ler
    effect: Effect
    fingerprint: str

@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    call: ResolvedCall | None
    reason: str
    model_message: str         # modele gidecek, sır içermeyen metin
    user_prompt: str | None    # kullanıcıya sorulacak: araç, argümanlar, hedef
    request_id: str | None     # iç kimlik; modele verilmez

@dataclass(frozen=True)
class ToolSpec:
    name: str
    resolve: "Callable[[dict, Source, dict], ResolvedCall]"   # eylem/hedef çözümleyici
    effect_of: "Callable[[ResolvedCall], Effect]"             # ör. file_controller eylem tablosu
    allowed_sources: frozenset[Source]
    auto_allow: "Callable[[ResolvedCall, Source], bool]" = lambda c, s: c.effect is Effect.READ
    fn: "Callable[[dict], str]" = ...                          # gerçek araç fonksiyonu

def register(spec: ToolSpec) -> None: ...
def authorize(tool: str, args: dict, source: Source, *, context: dict | None = None) -> Decision: ...
def execute(decision: Decision, grant: "Grant | None" = None) -> str: ...
# Girdi katmanı (main.py / ui / cli) çağırır; model yolundan erişilemez:
def user_turn(text: str, channel: str) -> "UserTurn": ...
def answer(turn: "UserTurn") -> "Grant | Denial | None": ...  # evet/hayır → en son duyurulan istek
```

**Karar tablosu (varsayılan):**

| Durum | Karar |
|-------|-------|
| Kayıtsız araç ya da kaynak izinli değil | DENY |
| Hedef politikayı ihlal ediyor (korumalı dosya, yazma kökü dışı, $HOME dışı) | DENY |
| Effect = READ ve hassas yol değil | ALLOW |
| Effect = READ ve hassas yol (`~/.ssh`, API anahtar dosyaları …) | NEEDS_APPROVAL |
| `auto_allow` açıkça izin veriyor (ör. ses/parlaklık) | ALLOW |
| MUTATE / EXTERNAL / EXECUTE / SYSTEM | NEEDS_APPROVAL |
| NEEDS_APPROVAL ama kaynak SCHEDULER/PROACTIVE/REACT | Kuyruğa alınır, kullanıcıya duyurulur; o turda çalışmaz |

### 4.3 Kapıya taşınacaklar (tek kaynak olacak)

| Bugünkü | Yer | Kapıdaki karşılığı |
|---------|-----|--------------------|
| `READONLY_ACTIONS`, `ACTION_ALIASES`, `normalize_action`, `is_readonly_action` | `file_controller.py:1427-1456` | `file_controller` ToolSpec `resolve` / `effect_of` |
| `_write_roots`, `_is_safe_write_path`, `_is_safe_write_file`, `_is_safe_path` | `file_controller.py:106-163` | `security_gate.paths` (araç içinde de çağrılmaya devam eder) |
| `check_write_target` | `code_helper.py:187` | `security_gate.paths` |
| `PROTECTED_FILES`, `_is_allowed_target` | `self_improve.py:69-100` | `security_gate.paths.protected` |
| `$HOME` sınırı | `tools/developer/agentic_coder.py:699`, `core/agent.py:40` | `security_gate.paths` (yazma kökleriyle birleştirilmeli) |
| `is_destructive`, `_READONLY_TOOLS`, `_SAFE_SETTINGS_ACTIONS` | `tools_kopru.py:208-250` | ToolSpec `effect_of` / `auto_allow` |
| `needs_confirmation`, `is_destructive`, tablolar | `tool_gate.py:20-40` | ToolSpec effect |
| `classify_risk`, `_HIGH_RISK`, `_MEDIUM_RISK`, `_normalize_action` | `security_ai.py:25-135` | ToolSpec effect; security_ai yalnızca açıklama metni üretir |
| `_HIGH_RISK_KEYWORDS` | `brain_orchestrator.py:115` | Yükseltme kuralı (yalnızca yukarı) |
| `_is_readonly` | `terminal_tool.py:60` | `terminal` ToolSpec `effect_of` |
| `_DANGEROUS_ACTIONS` | `computer_settings.py:586` | `computer_settings` ToolSpec |
| Registry seviyeleri, `SecurityManager.check` | `tools/security.py:80`, kayıt yerleri | ToolSpec effect (registry kaydı spec üretir) |
| `_is_confirmation`, `_is_rejection`, `_normalize_confirmation`, kelime listeleri | `main.py:980-1020` | `security_gate.user_turn` / `answer` |
| `_action_fingerprint`, `_set_pending_dangerous`, `_grant_dangerous_confirmation`, `_consume_dangerous_confirmation`, `_confirmation_granted_for` | `main.py:1021-1070` | `ApprovalStore` (çoklu kayıt) |
| `ApprovalService` | `core/approval_service.py` | `ApprovalStore` |
| Brain/agent_loop duyuru kancaları (`request_brain_team_approval`, `request_agent_loop_approval`, `_handle_*_reply`) | `main.py:1169-1360` | Kapının duyuru/cevap API'si (tek işleyici) |
| `expected_action` karşılaştırması | `agent_loop.py:238` | `Grant` → `ResolvedCall` eşleşmesi |
| Rate limit | `terminal_tool.py:115`, `tools/security.py:140` | Kapı (araç başına) |

### 4.4 Silinecekler (kapı devreye girdikten sonra)

- **`tool_gate.py`**: `gate`, `needs_confirmation`, `is_destructive`. `audit_entry`
  kapının denetim kaydına taşınır.
- **`main.py` onay mekanizmaları:**
  - kod-saklama mekanizması: `_CONFIRM_CODE_TOOLS`, `_CONFIRM_CODE_RE`, `_TERMINAL_CODE_RE`, `_tool_confirm_code`, `_prepare_confirmed_args`, `_redact_confirmation_result`, `_redact_terminal_result`, `_strip_terminal_code`, `_discard_tool_confirm_code`
  - `_pending_terminal_command` ve onaylı terminal dalı (`:1612`)
  - eski parçalar: ölü `_check_user_approval` / `_APPROVE_KEYWORDS` / `_DENY_KEYWORDS` / `_tool_approved` (`:2690-2705`) ve `main.py:2186` gate dalı
- **Araç içi onay kodları:**
  - `file_controller`: `_pending_file_ops`, `_pending_bulk_deletes`, `confirm_code` parametreleri
  - `code_helper`: `_pending_code_edits`, `_apply_confirmed_edit`
  - `self_improve`: `_pending_self_improve`
  - `dev_agent` / `agent_board`: `note_user_turn`, `confirmation_problem`, `_pending_dev_agent`, `confirm_code`
  - terminal_tool'un `approval_service` kullanımı
- **Araç içi onay bayrakları:** `computer_settings` ve `game_updater`'daki
  `_user_confirmation_granted`; yerini kapının verdiği `Grant` alır.
- **Ara geçici çözümler:**
  - `tools_kopru.call_approved_tool` ve `_APPROVED_TOOL` contextvar
  - `tools/security._sanitize_inputs` (regex listesi gerçek koruma sağlamıyor; istenirse
    kapıda yalnızca savunma derinliği olarak kalır)
- **CLI:** `core/agent.py` `_is_self_repair` / `_atomic_self_repair` onaysız yolu.
  `_ask_confirmation` CLI'nin `UserTurn` sağlayıcısı olur.
- **Şemalar:** Gemini ve registry şemalarındaki tüm `confirm_code` alanları
  (`start_parallel_task`, `dev_agent`).

### 4.5 Kalacaklar (araç içi savunma derinliği)

- `file_controller` içindeki yol kontrolleri; kapının `paths` modülünü çağırır, ikinci kez kontrol eder
- `windows_shell._ALLOWED_COMMANDS`, terminalde `shell=False`, zip-slip ve symlink kontrolleri
- `executor_ai._ALLOWED_ACTIONS`; çözümleme allowlist'i olarak, karar değil
- `entegrasyon._verify_module` statik doğrulaması
- Dashboard kimlik doğrulaması

### 4.6 Geçiş sırası

Her adımda kural aynı: önce stub'sız ve eski kodda kırmızı testler, sonra değişiklik.
Testler `HOME`/`JARVIS_HOME`'u tmp'ye yönlendirir; ağ ve LLM çağrısı yoktur.

| Adım | İş | Kapanan |
|------|----|---------|
| 0 | `security_gate.py` iskeleti, `Decision`/`ResolvedCall`/`ApprovalStore`. Bugünkü `main` yuvasını saran bir adaptör. Davranış değişmez; sözleşme testleri yazılır | — |
| 1 | `TOOL_SPECS`: §3'teki her araç için spec; mevcut tablolardan üretilir. Tutarlılık testi: **herhangi bir yoldan erişilebilen her araç adının bir spec'i olmalı**, yoksa test kırmızı | Tablolar arası kayma |
| 2 | E1 (sesli araç) ve E2/E3 (registry, ReAct) `authorize`/`execute` üzerinden geçer. `tool_gate` ve kod-saklama mekanizması kalkar | A, 3. bulgu, B'nin E1 kısmı |
| 3 | E5 ve E6: `is_destructive` ve `_risk_of_step`/`classify_risk` yerine `authorize(source=AGENT_LOOP / BRAIN_TEAM)`. Brain/agent duyuruları kapının duyuru API'sine geçer. `ApprovalStore` çoklu kayıt | G, 9. bulgu, F (coder_ai hedefleri `paths` ile) |
| 4 | Araç içi kodların silinmesi (§4.4). Araç fonksiyonları yalnızca `execute` üzerinden çağrılır; doğrudan import eden dağıtıcılar kaldırılır | Kalan çift onay yolları |
| 5 | E7/E4 terminal: yönlendirici `authorize(source=ROUTER)` kullanır; `approval_service` kalkar | — |
| 6 | E9 dev_agent / start_parallel_task, E15 CLI (self_repair bypass kalkar), E16 eklentiler (spec zorunlu; spec'siz eklenti aracı DENY) | B, C, D |
| 7 | Kaynak politikası: SCHEDULER/PROACTIVE/REACT için "kuyruğa al ve duyur". Dashboard metninin `UserTurn` üretip üretmeyeceği kararı (aşağıda) | E, H |
| 8 | Eski kodun ve testlerin temizliği; dokümantasyon | — |

### 4.7 Açık sorular (kullanıcı kararı gerekiyor)

1. **Dashboard metni onay verebilsin mi?** Kimliği doğrulanmış uzak kullanıcı.
   Telefon sesi bugün onay verebiliyor, metni veremiyor; ikisi tutarlı olmalı.
2. **Çoklu bekleyen onayda "evet" neyi onaylar?** Öneri: yalnızca en son duyurulan
   isteği. Ötekiler sırayla yeniden sorulur.
3. **Ses ve parlaklık gibi düşük riskli MUTATE'ler** `auto_allow` olarak kalsın mı?
   Bugün E5'te öyle.
4. **Hassas okuma yolları listesi:** `~/.ssh`, `~/.config/*/credentials`, Jarvis
   `api_keys.json`, tarayıcı profilleri, …
5. **`save_memory`:** Kalıcı belleğe yazma sistem istemine girdiği için kalıcı bir
   prompt-injection yüzeyi. Her yazma onaya mı bağlansın, yoksa yalnızca belirli
   kategoriler mi?
6. **Sesli kısmi transkript:** Onay yalnızca tamamlanmış turda mı verilsin?
   Bugün kısmi transkriptte veriliyor (`main.py:2778`).
