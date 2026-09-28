@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3.12"
if defined PYTHON_CMD goto python_found

where py >nul 2>&1
if not errorlevel 1 py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3.11"
if defined PYTHON_CMD goto python_found

where python3.12 >nul 2>&1
if not errorlevel 1 python3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=python3.12"
if defined PYTHON_CMD goto python_found

where python3.11 >nul 2>&1
if not errorlevel 1 python3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=python3.11"
if defined PYTHON_CMD goto python_found

echo Python 3.11 veya 3.12 bulunamadi. Python Launcher veya python.org kurulumu gerekir.
exit /b 1

:python_found
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto check_venv

%PYTHON_CMD% -m venv .venv
if errorlevel 1 (
    echo .venv olusturulamadi.
    exit /b 1
)
goto install

:check_venv
set "VENV_VERSION="
for /f "delims=" %%V in ('call "%VENV_PY%" -c "import sys;print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1]))" 2^>nul') do set "VENV_VERSION=%%V"
if "%VENV_VERSION%"=="3.11" goto install
if "%VENV_VERSION%"=="3.12" goto install

echo Mevcut .venv Python %VENV_VERSION% ve desteklenen 3.11/3.12 ile uyusmuyor.
echo .venv silinmedi; elle duzeltin veya bu projeyi baska bir klasore kopyalayin.
exit /b 1

:install
"%VENV_PY%" -m pip install -e ".[dev]"
if errorlevel 1 (
    echo Paket kurulumu basarisiz.
    exit /b 1
)
"%VENV_PY%" scripts\post_install_check.py
if errorlevel 1 (
    echo Offline paket kontrolu basarisiz.
    exit /b 1
)
echo Kurulum tamamlandi. Baslatmak icin BASLAT_WINDOWS.cmd kullanin.
exit /b 0
