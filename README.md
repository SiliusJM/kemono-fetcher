# kemono-fetcher 🖼️

**Herramienta forense de recuperación de galería para [Kemono.cr](https://kemono.cr) y [Coomer.cr](https://coomer.cr)**

Recupera imágenes y vídeos de posts aunque el almacenamiento principal del servidor esté caído, usando el CDN de miniaturas como fallback y guardando las URLs full-res para cuando el servidor vuelva online.

---

## 📋 Requisitos

| Requisito | Versión mínima |
|-----------|----------------|
| Python    | 3.11+          |
| pip       | incluido con Python |

> ⚠️ **Python debe estar instalado manualmente** desde [python.org](https://www.python.org/downloads/). Todo lo demás se instala automáticamente.

---

## 🚀 Inicio rápido (Windows)

### Opción A — Doble clic (recomendado)
```
kemono_launch.bat
```
El `.bat` instala dependencias, instala el navegador Chromium y te pide la URL del post.

### Opción B — Línea de comandos
```bash
# Instalar dependencias (solo la primera vez)
pip install -r requirements_galeria.txt
python -m playwright install chromium

# Uso básico
python kemono_galeria.py "https://kemono.cr/patreon/user/USER/post/POST"

# Con nombre personalizado para el ZIP
python kemono_galeria.py "<url>" --zip-name "Zenith Greyrat [XTRAS] (Patreon)"
python kemono_galeria.py "<url>" --zip-name "Zenith_Greyrat_xtrapack1_Deik0.zip"

# Más opciones
python kemono_galeria.py "<url>" --output ./mi_carpeta --concurrency 8
python kemono_galeria.py "<url>" --skip-playwright   # reutilizar cache de sesión anterior
python kemono_galeria.py "<url>" --skip-network      # omitir análisis TCP/DNS (más rápido)
```

---

## ⚙️ Opciones disponibles

| Argumento | Descripción | Default |
|-----------|-------------|---------|
| `url` | URL del post de Kemono/Coomer | requerido |
| `--output CARPETA` | Carpeta donde guardar todo | `kemono_galeria_output` |
| `--zip-name NOMBRE` | Nombre personalizado del ZIP de salida | auto (usa ID del post) |
| `--concurrency N` | Descargas simultáneas | `6` |
| `--no-zip` | No crear archivo ZIP (mantener solo las imágenes) | — |
| `--skip-network` | Omitir análisis TCP/DNS (más rápido, sin diagnóstico) | — |
| `--continue-on-error` | Continuar aunque Playwright falle | — |

---

## 📂 Archivos generados

Después de ejecutar el tool, encontrarás esto en la carpeta de salida:

Cada descarga crea su propia subcarpeta basada en el post ID:

```
kemono_galeria_output/
└── patreon_142198624/              ← subcarpeta por post (service_postid)
    ├── images/
    │   ├── 0001.jpg
    │   ├── 0002.png
    │   └── ...
    ├── recovered_patreon_..._142198624.zip   ← ZIP con imágenes + manifest
    ├── failed.txt                            ← Solo se crea si hay errores
    └── _cache/                               ← Archivos técnicos (ignorar)
        ├── metadata.json
        ├── fullres_urls.txt
        └── extracted_urls.json
```

> Descargar posts diferentes no mezcla sus imágenes.

---

## ⚡ Las 7 fases de recuperación

```
FASE 1  →  Análisis de red (DNS, TCP, TLS, HTTP de todos los hosts de Kemono)
FASE 2  →  Extracción Playwright (navega el post con Chromium real, captura toda la red)
FASE 3  →  Clasificación y validación (full-res vs thumbnail, HEAD request por cada URL)
FASE 4  →  Descarga (async, retries con backoff, resume parcial, fallback automático)
FASE 5  →  Exportar (metadata.json, fullres_urls.txt, failed.txt)
FASE 6  →  ZIP (empaqueta todo con manifest.json interno)
FASE 7  →  Veredicto (tabla de estado + diagnóstico + próximos pasos)
```

### ⏱️ Tiempos reales (medidos en Windows)

| Situación | Tiempo aproximado |
|-----------|-------------------|
| Primera descarga de un post nuevo | ~27 segundos |
| Mismo autor, post diferente (red cacheada 1h) | ~15 segundos |
| Cualquier post, red ya cacheada | < 32 segundos |

> El análisis de red (Fase 1) se guarda en caché 1 hora. Playwright (Fase 2) también se cachea por post — si vuelves a descargar el mismo post, se salta directo a la descarga.

### Verdicts posibles

| Código | Significado |
|--------|-------------|
| **A** | ✅ Recuperación completa — imágenes/vídeos en resolución original |
| **B** | 🟡 Solo thumbnails — storage offline, miniaturas descargadas |
| **C** | 🟡 Parcial — URLs identificadas pero ninguna descargable ahora |
| **D** | 🔴 Sitio completamente offline |
| **E** | 🔴 Sin media detectada — verificar URL |

---

## ❓ ¿Por qué solo se descargan miniaturas y no las imágenes completas?

### Arquitectura de Kemono

Kemono usa **dos sistemas de almacenamiento separados**:

```
┌─────────────────────────────────────────────────────────┐
│  FRONTEND  kemono.cr   190.115.31.240   ✅ ONLINE       │
│  CDN THUMB img.kemono.cr                ✅ ONLINE       │
│                                                         │
│  STORAGE   n1.kemono.cr  91.149.227.10  ❌ OFFLINE      │
│            n2.kemono.cr  91.149.227.11  ❌ OFFLINE      │
│            n3.kemono.cr  91.149.227.12  ❌ OFFLINE      │
│            n4.kemono.cr  91.149.227.13  ❌ OFFLINE      │
└─────────────────────────────────────────────────────────┘
```

- **Thumbnails** (miniaturas) → servidas por `img.kemono.cr/thumbnail/data/` → **CDN vivo**
- **Imágenes full-res** → almacenadas en `n1-n4.kemono.cr/data/` → **nodos offline**

Los nodos `91.149.227.10-13` **no responden a ningún TCP SYN** desde ningún punto del mundo (confirmado con 24 nodos de prueba globales). Es un problema de **infraestructura del servidor**, no de tu red ni de geobloqueo.

### ¿Qué hace el tool cuando el storage está offline?

1. Intenta descargar full-res desde `img.kemono.cr/data/` (misma CDN, sin `/thumbnail/`)
2. Intenta los nodos `n1-n4.kemono.cr` directamente
3. Si ambos fallan → **fallback automático** al thumbnail (misma imagen comprimida)
4. Guarda las URLs full-res en `fullres_urls.txt` para reintentar cuando el servidor vuelva

### ¿Cuándo volverá el storage online?

No se sabe. Depende de los administradores de Kemono. El tool guarda las URLs exactas de full-res para que cuando los nodos vuelvan, puedas ejecutar:

```bash
python kemono_galeria.py "<misma URL>" --skip-playwright
```

Y descargará automáticamente las versiones completas.

---

## 🎬 Limitaciones actuales

### Vídeos
- Los vídeos están almacenados en los **mismos nodos n1-n4** que las imágenes
- Mientras el storage esté offline → **imposible descargar vídeos**
- El tool **detecta y lista** los vídeos encontrados pero no puede descargarlos
- Cuando el storage vuelva online, **el tool descargará vídeos automáticamente** sin cambios

### Imágenes: thumbnail vs full-res

| | Thumbnail | Full-res |
|---|---|---|
| **Resolución** | ~800px (comprimida) | Original (puede ser 4K+) |
| **Disponibilidad** | ✅ Siempre | ❌ Solo cuando n1-n4 estén online |
| **Formato servido** | JPEG/WebP (recomprimido) | Formato original del creador |

### Otros
- La API de Kemono requiere cookies de sesión → el tool usa **Playwright con Chromium real** para obtener las cookies automáticamente
- Algunos posts con contenido de solo texto o PDFs pueden mostrar pocos items

---

## 🔄 ¿Funcionará también para vídeos e imágenes completas cuando el storage vuelva?

**Sí.** El tool ya tiene todo el código implementado:
- Detecta extensiones de vídeo (`.mp4`, `.webm`, `.mov`, `.avi`, `.mkv`, etc.)
- Usa timeouts extendidos para archivos grandes (`TIMEOUT_VID = 120s`)
- Soporta downloads con resume (header `Range: bytes=N-`)
- Activa fallback full-res → thumbnail solo si el host está marcado como muerto

Cuando los nodos `n1-n4` vuelvan online, el tool descargará **tanto imágenes como vídeos en calidad original** sin modificaciones.

---

## 🛠️ Stack técnico

- [`playwright`](https://playwright.dev/python/) — navegador Chromium headless
- [`aiohttp`](https://docs.aiohttp.org/) — HTTP async con retries y resume
- [`aiofiles`](https://github.com/Tinche/aiofiles) — escritura async de archivos
- [`rich`](https://rich.readthedocs.io/) — consola con barra de progreso y tablas
- [`beautifulsoup4`](https://www.crummy.com/software/BeautifulSoup/) — parsing HTML auxiliar

---

## 📝 Licencia

MIT — uso libre, sin garantías.
