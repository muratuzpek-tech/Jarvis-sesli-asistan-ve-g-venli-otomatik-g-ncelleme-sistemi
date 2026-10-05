"""
CanFPV Jarvis — Yedekleme ve Geri Alma Aracı

Manuel bir araçtır: hiçbir şeyi otomatik çalıştırmaz, internetten hiçbir şey
indirmez, ne değiştireceğine kendi kendine karar vermez. Sen çalıştırırsın,
sen kontrol edersin.

KÖK DİZİN TEK YERDEN GELİR: jarvis_project_root() (Jarvis paketi, yani
src/jarvis; donmuş/paketlenmiş sürümde çalıştırılabilir dosyanın klasörü).
Yedekler projenin TAMAMEN DIŞINDA, yanındaki "<ad>_yedekler" klasöründe,
zaman damgalı ayrı klasörlerde tutulur (backups_root_for()).

İKİ TÜR YEDEK:
  * Proje yedeği (create_backup / rollback): project_path'in TAMAMI
    (__pycache__ ve *.pyc hariç). Kullanıcı verisi (JARVIS_HOME), depo
    kökündeki tests/ tools/ docs/ ve proje dışındaki dosyalar KAPSAM DIŞIDIR.
  * Hedef dosya yedeği (backup_file / restore_file): proje dışında da olabilen
    TEK bir dosya (ör. Brain Team'in değiştireceği hedef). Geri yükleme
    yalnızca o dosyayı geri koyar; projeye dokunmaz.

GÜVENLİK: rollback atomiktir (hazırla -> eskiyi yana taşı -> yenisini koy);
herhangi bir adım başarısız olursa eski proje yerine konur ve False döner.
rollback yalnızca backups_root altındaki, dizin olan ve boş olmayan bir proje
yedeğini kabul eder; aksi halde HİÇBİR ŞEYE dokunmadan reddeder. Hiçbir adım
hata yutup "tamamlandı" demez.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Proje yedeği klasör adları: 20260101-120000, 20260101-120000-1,
# 20260101-120000-rollback-oncesi. Gizli (".") ve "_" ile başlayanlar yedek değil.
_BACKUP_NAME = re.compile(r"^\d{8}-\d{6}(?:-[\w-]+)?$")
_FILE_BACKUPS_DIR = "_hedef_dosyalar"
_FILE_MANIFEST = "manifest.json"


def jarvis_project_root() -> Path:
    """Yedeklenen/geri alınan Jarvis klasörü - TEK kaynak (executor_ai,
    self_improve, entegrasyon, Brain Team ve bu dosyanın __main__'i)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def backups_root_for(project_path: Path) -> Path:
    project_path = Path(project_path).resolve()
    return project_path.parent / f"{project_path.name}_yedekler"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class JarvisBackupTool:
    """Jarvis proje yedeği ve hedef dosya yedeği.

    Kapsam: proje yedeği project_path'in tamamıdır (for_jarvis() için
    src/jarvis paketi); hedef dosya yedeği tek bir dosyadır. Ayrıntı için
    modül belgesine bakın."""

    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path).resolve()
        self.backups_root = backups_root_for(self.project_path)
        self.version_file = self.project_path / "version.json"
        self.last_error: str | None = None

    @classmethod
    def for_jarvis(cls) -> "JarvisBackupTool":
        """Jarvis'in kendi klasörü (src/jarvis) için araç. Kapsam: yalnızca
        bu paket; kullanıcı verisi ve depo kökü kapsam dışı."""
        return cls(jarvis_project_root())

    # ── Sürüm takibi ─────────────────────────────────────────────────────
    def get_current_version(self) -> str:
        if not self.version_file.exists():
            return "1.0.0"
        try:
            data = json.loads(self.version_file.read_text(encoding="utf-8"))
            return data.get("version", "1.0.0")
        except Exception:
            return "1.0.0"

    def update_version(self, new_version: str) -> None:
        data = {"version": new_version, "updated_at": datetime.now().isoformat()}
        self.version_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"✓ Sürüm güncellendi: {new_version}")

    # ── Proje yedeği ─────────────────────────────────────────────────────
    def _unique_dir(self, name: str) -> Path:
        destination = self.backups_root / name
        counter = 1
        while destination.exists() or destination.is_symlink():
            destination = self.backups_root / f"{name}-{counter}"
            counter += 1
        return destination

    def create_backup(self) -> Path:
        """Projenin tam yedeği, zaman damgalı ayrı bir klasöre. Önceki
        yedekleri SİLMEZ. Kopyalama başarısız olursa yarım klasör bırakmaz ve
        hatayı yükseltir."""
        self.backups_root.mkdir(parents=True, exist_ok=True)
        destination = self._unique_dir(_stamp())
        print(f"Yedek oluşturuluyor: {destination}")
        try:
            shutil.copytree(
                self.project_path,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        print("✓ Yedek oluşturuldu.")
        return destination

    def list_backups(self) -> list[Path]:
        """Proje yedekleri, en yeniden en eskiye. Hedef dosya yedekleri ve
        geçici klasörler dahil DEĞİL."""
        if not self.backups_root.exists():
            return []
        return sorted(
            (p for p in self.backups_root.iterdir()
             if p.is_dir() and not p.is_symlink() and _BACKUP_NAME.match(p.name)),
            key=lambda p: p.name, reverse=True,
        )

    def cleanup_old_backups(self, keep: int = 10) -> int:
        """En yeni `keep` proje yedeği dışındakileri siler; silme hataları
        yükseltilir. Kaç tane silindiğini döndürür."""
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
            raise ValueError(f"keep negatif olmayan bir tam sayı olmalı: {keep!r}")
        to_delete = self.list_backups()[keep:]
        for backup in to_delete:
            shutil.rmtree(backup)
        if to_delete:
            print(f"✓ {len(to_delete)} eski yedek temizlendi.")
        return len(to_delete)

    # ── Geri alma ────────────────────────────────────────────────────────
    def _validate_project_backup(self, backup_path: Path) -> str | None:
        """Geçerli bir proje yedeği değilse sebebi, geçerliyse None."""
        raw = Path(backup_path)
        if raw.is_symlink():
            return f"yedek sembolik link olamaz: {raw}"
        try:
            resolved = raw.resolve()
        except OSError as exc:
            return f"yedek yolu çözümlenemedi: {exc}"
        root = self.backups_root.resolve()
        if resolved.parent != root:
            return f"yedek {root} klasörünün doğrudan altında değil: {resolved}"
        if not _BACKUP_NAME.match(resolved.name):
            return f"proje yedeği adı değil: {resolved.name}"
        if not resolved.is_dir():
            return f"yedek bir klasör değil: {resolved}"
        if not any(resolved.iterdir()):
            return f"yedek boş: {resolved}"
        return None

    def _fail(self, message: str) -> bool:
        self.last_error = message
        print(f"✗ Rollback başarısız: {message}")
        return False

    def rollback(self, backup_path: Path | None = None) -> bool:
        """Belirtilen yedeği (verilmezse EN YENİ yedeği) atomik olarak geri
        yükler:
          1. yedek doğrulanır (backups_root altında, klasör, boş değil),
          2. yedek gizli bir geçici klasöre kopyalanır (proje henüz değişmez),
          3. mevcut proje yan klasöre ("<damga>-rollback-oncesi") TAŞINIR,
          4. hazırlanan kopya projenin yerine konur.
        2-4'ten biri başarısız olursa eski proje yerine konur ve False döner.
        Geri alma öncesi durum yedekler arasında korunur (silinmez)."""
        self.last_error = None
        if backup_path is None:
            backups = self.list_backups()
            if not backups:
                return self._fail("hiç yedek bulunamadı")
            backup_path = backups[0]
        problem = self._validate_project_backup(Path(backup_path))
        if problem:
            return self._fail(problem)
        backup_path = Path(backup_path).resolve()

        stamp = _stamp()
        staging = self.backups_root / f".rollback-hazirlik-{stamp}"
        aside = self._unique_dir(f"{stamp}-rollback-oncesi")
        print(f"Geri alınıyor: {backup_path}")

        # 2) hazırla
        try:
            shutil.copytree(backup_path, staging)
        except Exception as exc:
            try:
                shutil.rmtree(staging)
            except FileNotFoundError:
                pass
            except Exception as cleanup_exc:
                return self._fail(f"yedek hazırlanamadı ({exc}); geçici klasör de "
                                  f"silinemedi ({cleanup_exc}): {staging}")
            return self._fail(f"yedek hazırlanamadı: {exc}")

        # 3) mevcut projeyi yana taşı
        had_project = self.project_path.exists()
        if had_project:
            try:
                os.replace(self.project_path, aside)
            except OSError as exc:
                try:
                    shutil.rmtree(staging)
                except Exception as cleanup_exc:
                    return self._fail(f"mevcut proje yana taşınamadı ({exc}); proje "
                                      f"DEĞİŞMEDİ; geçici klasör silinemedi ({cleanup_exc})")
                return self._fail(f"mevcut proje yana taşınamadı, proje DEĞİŞMEDİ: {exc}")

        # 4) yenisini yerine koy
        try:
            os.replace(staging, self.project_path)
        except OSError as exc:
            if had_project:
                try:
                    os.replace(aside, self.project_path)
                except OSError as restore_exc:
                    self.last_error = (f"yeni sürüm yerine konamadı ({exc}) VE eski proje "
                                       f"geri konamadı ({restore_exc}); eski proje: {aside}")
                    raise RuntimeError(self.last_error) from restore_exc
            return self._fail(f"yedek yerine konamadı, eski proje geri kondu: {exc}")

        print(f"✓ Rollback tamamlandı. Geri alma öncesi durum: {aside if had_project else '—'}")
        return True

    # ── Hedef dosya yedeği ───────────────────────────────────────────────
    def backup_file(self, target: str | Path) -> Path:
        """TEK bir dosyanın yedeği (proje dışında da olabilir). Dönen yol
        restore_file()'a verilir. Hata yükseltilir - çağıran değişikliği
        yapmamalıdır."""
        target = Path(target).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"yedeklenecek dosya yok: {target}")
        store = self.backups_root / _FILE_BACKUPS_DIR
        store.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"{_stamp()}-", dir=store))
        try:
            shutil.copy2(target, folder / "dosya")
            (folder / _FILE_MANIFEST).write_text(json.dumps(
                {"original": str(target), "created_at": datetime.now().isoformat()},
                ensure_ascii=False), encoding="utf-8")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return folder

    def restore_file(self, backup: str | Path) -> bool:
        """backup_file() yedeğini, manifestteki ASIL yola atomik olarak geri
        koyar (aynı klasörde geçici dosya + os.replace). Yalnızca
        backups_root/_hedef_dosyalar altındaki yedekleri kabul eder."""
        self.last_error = None
        folder = Path(backup)
        store = (self.backups_root / _FILE_BACKUPS_DIR).resolve()
        if folder.is_symlink() or folder.resolve().parent != store:
            return self._fail(f"hedef dosya yedeği {store} altında değil: {folder}")
        folder = folder.resolve()
        data_file, manifest = folder / "dosya", folder / _FILE_MANIFEST
        if not data_file.is_file() or not manifest.is_file():
            return self._fail(f"hedef dosya yedeği eksik: {folder}")
        try:
            original = Path(json.loads(manifest.read_text(encoding="utf-8"))["original"])
        except Exception as exc:
            return self._fail(f"manifest okunamadı: {exc}")
        tmp = original.parent / f".{original.name}.geri-yukleme"
        try:
            shutil.copy2(data_file, tmp)
            os.replace(tmp, original)
        except OSError as exc:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            return self._fail(f"dosya geri yüklenemedi ({original}): {exc}")
        print(f"✓ Dosya geri yüklendi: {original}")
        return True


if __name__ == "__main__":
    tool = JarvisBackupTool.for_jarvis()

    print()
    print("=" * 40)
    print("   CANFPV JARVIS — YEDEKLEME ARACI")
    print("=" * 40)
    print()
    print("Proje:", tool.project_path)
    print("Yedek klasörü:", tool.backups_root)
    print("Mevcut sürüm:", tool.get_current_version())
    print("Kayıtlı yedek sayısı:", len(tool.list_backups()))
    print()
    print("Kullanım (Python'dan içe aktararak):")
    print("  tool.create_backup()        — yeni bir yedek al")
    print("  tool.list_backups()         — yedekleri listele")
    print("  tool.rollback()             — en son yedeğe geri dön")
    print("  tool.cleanup_old_backups()  — eski yedekleri temizle")
