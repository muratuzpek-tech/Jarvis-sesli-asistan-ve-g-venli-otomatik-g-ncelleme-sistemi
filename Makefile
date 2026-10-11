# MuratJARVIS — Ortak Komut Kısayolları
# Kullanım: make install | make test | make lint | make docker-build

.PHONY: install install-dev test lint lint-fix format clean docker-build docker-run

# ── Kurulum ─────────────────────────────────────────────
install:
	python -m pip install --upgrade pip
	pip install -e .

install-dev:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

# ── Test ────────────────────────────────────────────────
test:
	python -m pytest

test-verbose:
	python -m pytest -v --tb=short

# ── Lint ────────────────────────────────────────────────
lint:
	ruff check .

lint-fix:
	ruff check --fix .

format:
	ruff format src/

# ── Build ───────────────────────────────────────────────
clean:
	rm -rf dist/ build/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete

build:
	python -m build

# ── Docker ──────────────────────────────────────────────
docker-build:
	docker build -t jarvis-dashboard .

docker-run:
	docker run -p 8080:8080 jarvis-dashboard

# ── Docs ────────────────────────────────────────────────
help:
	@echo "MuratJARVIS — Ortak Komutlar:"
	@echo ""
	@echo "  make install       — Paketi kur (runtime deps)"
	@echo "  make install-dev   — Paketi kur (dev deps: pytest, ruff)"
	@echo "  make test          — Testleri çalıştır"
	@echo "  make test-verbose  — Detaylı test çıktısı"
	@echo "  make lint          — Lint kontrolü"
	@echo "  make lint-fix      — Lint sorunlarını otomatik düzelt"
	@echo "  make format        — Kodu biçimlendir"
	@echo "  make clean         — Build artefaktlarını temizle"
	@echo "  make build         — Python wheel/sdist oluştur"
	@echo "  make docker-build  — Docker image oluştur"
	@echo "  make docker-run    — Dashboard container çalıştır"
