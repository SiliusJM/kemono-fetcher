@echo off
setlocal enabledelayedexpansion
title kemono-fetcher
chcp 65001 >nul 2>&1

echo.
echo  ==================================================
echo    kemono-fetcher  ^|  Gallery Recovery Tool
echo    https://github.com/SiliusJM/kemono-fetcher
echo  ==================================================
echo.

:: PASO 1 - Verificar Python
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

:: PASO 2 - pip disponible?
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

:: PASO 3 - Instalar dependencias
echo.
echo [*] Verificando dependencias...

if not exist "%~dp0requirements_galeria.txt" (
    echo [ERROR] No se encontro requirements_galeria.txt junto al .bat
    pause
    exit /b 1
)

python -m pip install -q -r "%~dp0requirements_galeria.txt"
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Fallo al instalar dependencias. Verifica tu conexion a internet.
    echo.
    pause
    exit /b 1
)
echo [OK] Dependencias listas

:: PASO 4 - Chromium para Playwright
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

:: PASO 5 - Verificar kemono_galeria.py
if not exist "%~dp0kemono_galeria.py" (
    echo.
    echo [ERROR] No se encontro kemono_galeria.py junto al .bat
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0"

:: BUCLE PRINCIPAL
:LOOP

echo.
echo  -------------------------------------------------------
echo   Pega la URL del post de Kemono o Coomer y presiona Enter.
echo   Ejemplo: https://kemono.cr/patreon/user/101779509/post/109629764
echo.
set POST_URL=
set /p POST_URL="  URL: "
echo.

if "!POST_URL!"=="" (
    echo [WARN] No ingresaste ninguna URL. Intenta de nuevo.
    goto LOOP
)

echo !POST_URL! | findstr /i "kemono.cr coomer.cr" >nul
if %errorlevel% neq 0 (
    echo [WARN] La URL no parece ser de kemono.cr ni coomer.cr. Continuando...
    echo.
)

:: ZIP - preguntar al usuario
echo  [Opcional] Nombre del ZIP de salida.
echo   - Deja vacio para nombre automatico.
echo   - Escribe NO si NO quieres ZIP (ya tienes las imagenes).
echo   - O escribe el nombre: Ej.  Zenith Greyrat [XTRAS] (Patreon)
echo.
set CUSTOM_ZIP=
set ZIP_NAME_ARG=
set NO_ZIP_ARG=
set /p CUSTOM_ZIP="  Nombre del ZIP [Enter/NO/nombre]: "
echo.

if /i "!CUSTOM_ZIP!"=="NO" (
    set NO_ZIP_ARG=--no-zip
) else if not "!CUSTOM_ZIP!"=="" (
    set ZIP_NAME_ARG=--zip-name "!CUSTOM_ZIP!"
)

:: Ejecutar
echo.
echo  Iniciando descarga...
echo  URL: !POST_URL!
echo.

python kemono_galeria.py "!POST_URL!" !ZIP_NAME_ARG! !NO_ZIP_ARG!
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE% equ 0 (
    echo [OK] Proceso completado sin errores.
) else (
    echo [WARN] El proceso termino con codigo %EXIT_CODE%. Revisa los mensajes anteriores.
)

:: Continuar o salir
echo.
echo  -------------------------------------------------------
echo   Que deseas hacer?
echo     1  -  Descargar otra URL
echo     2  -  Salir
echo  -------------------------------------------------------
echo.
set OPCION=
set /p OPCION="  Opcion [1/2]: "

if "!OPCION!"=="1" goto LOOP
if "!OPCION!"=="2" goto FIN

echo [WARN] Opcion invalida. Escribe 1 o 2.
timeout /t 2 >nul
goto LOOP

:FIN
echo.
echo  Hasta luego!
echo.
endlocal