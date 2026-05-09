@echo off
setlocal enabledelayedexpansion
title kemono-fetcher
chcp 65001 >nul 2>&1

echo.
echo  ==================================================
echo    kemono-fetcher  ^|  Gallery Recovery Tool
echo    https://github.com/SILIUS/kemono-fetcher
echo  ==================================================
echo.

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 1 — Verificar Python
:: ─────────────────────────────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] Python no esta instalado o no esta en el PATH.
    echo.
    echo  Solucion:
    echo    1. Descarga Python 3.11 o superior desde:
    echo       https://www.python.org/downloads/
    echo    2. Durante la instalacion activa:
    echo       [x] Add Python to PATH
    echo    3. Vuelve a abrir esta ventana y ejecuta el .bat de nuevo.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] %PYVER% detectado

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 2 — pip disponible?
:: ─────────────────────────────────────────────────────────────────────────────
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] pip no encontrado, intentando instalarlo...
    python -m ensurepip --upgrade >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] No se pudo instalar pip. Reinstala Python con pip incluido.
        pause
        exit /b 1
    )
)

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 3 — Instalar dependencias desde requirements_galeria.txt
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [*] Instalando dependencias (puede tardar la primera vez)...

if not exist "%~dp0requirements_galeria.txt" (
    echo [ERROR] No se encontro requirements_galeria.txt junto al .bat
    echo         Asegurate de clonar o descargar todos los archivos del repositorio.
    pause
    exit /b 1
)

python -m pip install -q -r "%~dp0requirements_galeria.txt"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Fallo al instalar dependencias.
    echo         Verifica tu conexion a internet e intenta de nuevo.
    echo         Tambien puedes instalar manualmente:
    echo           pip install -r requirements_galeria.txt
    echo.
    pause
    exit /b 1
)
echo [OK] Dependencias instaladas

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 4 — Instalar Chromium para Playwright
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo [*] Verificando Chromium (navegador para Playwright)...
python -m playwright install chromium >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] No se pudo instalar Chromium.
    echo         Intenta manualmente:  python -m playwright install chromium
    pause
    exit /b 1
)
echo [OK] Chromium listo

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 5 — Verificar que kemono_galeria.py existe
:: ─────────────────────────────────────────────────────────────────────────────
if not exist "%~dp0kemono_galeria.py" (
    echo.
    echo [ERROR] No se encontro kemono_galeria.py junto al .bat
    echo         Asegurate de clonar o descargar todos los archivos del repositorio.
    echo.
    pause
    exit /b 1
)

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 6 — Obtener URL del post
:: ─────────────────────────────────────────────────────────────────────────────
echo.
if "%~1"=="" (
    echo  Pega la URL del post de Kemono o Coomer y presiona Enter.
    echo  Ejemplo: https://kemono.cr/patreon/user/101779509/post/109629764
    echo.
    set /p POST_URL="  URL: "
    echo.
) else (
    set POST_URL=%~1
)

if "!POST_URL!"=="" (
    echo [ERROR] No ingresaste una URL.
    pause
    exit /b 1
)

:: Validacion basica de URL
echo !POST_URL! | findstr /i "kemono.cr coomer.cr" >nul
if %errorlevel% neq 0 (
    echo [WARN] La URL no parece ser de kemono.cr ni coomer.cr
    echo        Continuando de todas formas...
    echo.
)

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 7 — Nombre personalizado del ZIP (opcional)
:: ─────────────────────────────────────────────────────────────────────────────
set ZIP_NAME_ARG=
if "%~2"=="" (
    echo  [Opcional] Nombre personalizado para el ZIP de salida.
    echo  Deja vacio para usar el nombre automatico ^(ID del post^).
    echo  Ejemplo: Zenith Greyrat [XTRAS] ^(Patreon^)
    echo.
    set /p CUSTOM_ZIP="  Nombre del ZIP (Enter para omitir): "
    if not "!CUSTOM_ZIP!"=="" (
        set ZIP_NAME_ARG=--zip-name "!CUSTOM_ZIP!"
    )
) else (
    set ZIP_NAME_ARG=--zip-name "%~2"
)

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 8 — Ejecutar la herramienta
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo  Iniciando recuperacion...
echo  URL: !POST_URL!
if not "!ZIP_NAME_ARG!"=="" echo  ZIP: !CUSTOM_ZIP!
echo.

cd /d "%~dp0"
python kemono_galeria.py "!POST_URL!" !ZIP_NAME_ARG! %3 %4 %5
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE% equ 0 (
    echo [OK] Proceso completado sin errores.
) else (
    echo [WARN] El proceso termino con codigo de salida %EXIT_CODE%.
    echo        Revisa los mensajes anteriores para mas detalles.
)

:: ─────────────────────────────────────────────────────────────────────────────
:: PASO 9 — Abrir carpeta de resultados
:: ─────────────────────────────────────────────────────────────────────────────
if exist "%~dp0kemono_galeria_output" (
    echo.
    echo [*] Abriendo carpeta de resultados...
    explorer "%~dp0kemono_galeria_output"
)

echo.
echo Presiona cualquier tecla para salir...
pause >nul
endlocal
