#!/usr/bin/env python3
"""
kemono_galeria.py  v2.0
========================
Herramienta forense de recuperación de media para Kemono / Coomer.
Recupera imágenes y vídeos incluso cuando el storage principal está caído.

Uso:
    python kemono_galeria.py "https://kemono.cr/SERVICE/user/UID/post/PID"
    python kemono_galeria.py "<url>" --output ./salida --concurrency 8
    python kemono_galeria.py "<url>" --skip-playwright   (reutilizar cache)
    python kemono_galeria.py "<url>" --skip-network      (omitir TCP/DNS)

Requisitos:
    pip install playwright aiohttp aiofiles rich httpx beautifulsoup4
    python -m playwright install chromium
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import argparse, asyncio, hashlib, http.client, json, os, re, socket, ssl
import struct, sys, time, traceback, zipfile
from dataclasses import dataclass, field
from datetime    import datetime
from pathlib     import Path
from typing      import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urljoin

# ── verificar dependencias críticas ──────────────────────────────────────────
_MISSING: List[str] = []
for _dep in ("aiohttp", "aiofiles", "rich"):
    try:
        __import__(_dep)
    except ImportError:
        _MISSING.append(_dep)
if _MISSING:
    print(f"\n[ERROR] Dependencias faltantes. Ejecuta:\n"
          f"  pip install {' '.join(_MISSING)}\n")
    sys.exit(1)

import aiohttp
import aiofiles
from rich.console  import Console
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TaskProgressColumn,
    TimeElapsedColumn, TimeRemainingColumn, DownloadColumn,
    TransferSpeedColumn, TextColumn, MofNCompleteColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich.text  import Text

try:
    from playwright.async_api import async_playwright
    _PLAYWRIGHT = True
except ImportError:
    _PLAYWRIGHT = False

try:
    from bs4 import BeautifulSoup
    _BS4 = True
except ImportError:
    _BS4 = False

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTES & CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

VERSION           = "2.0"
OUTPUT_DIR        = Path("kemono_galeria_output")
NETWORK_CACHE_TTL = 3600  # segundos — reutilizar análisis de red dentro de 1 hora

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

IMG_CDN      = "img.kemono.cr"
# Nodos storage 91.149.227.10-13 — pre-marcados como muertos hasta confirmar
DEAD_NODES: Set[str] = {f"n{i}.kemono.cr" for i in range(1, 7)}

MIRROR_HOSTS = [
    "kemono.cr", "www.kemono.cr",
    "img.kemono.cr",
    "n1.kemono.cr", "n2.kemono.cr", "n3.kemono.cr", "n4.kemono.cr",
    "coomer.cr", "bunkrr.su",
]

IMG_EXTS   = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".tiff", ".jxl"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v"}
MEDIA_EXTS = IMG_EXTS | VIDEO_EXTS | {".zip", ".rar", ".pdf", ".mp3"}

# Primeros bytes de cada formato (para validar descargas)
MAGIC: Dict[str, bytes] = {
    ".jpg":  b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".png":  b"\x89PNG",
    ".gif":  b"GIF8",
    ".webp": b"RIFF",
    ".mp4":  b"",           # estructura variable (ftyp box)
    ".webm": b"\x1a\x45\xdf\xa3",
    ".zip":  b"PK",
}

HASH64_RE    = re.compile(r"\b([0-9a-f]{64})\b", re.IGNORECASE)
CONCURRENCY  = 6
TIMEOUT_FAST = 4.0     # HEAD en hosts potencialmente vivos
TIMEOUT_DEAD = 1.5     # HEAD en hosts pre-marcados muertos
TIMEOUT_DL   = 45.0    # GET archivos normales
TIMEOUT_VID  = 120.0   # GET vídeos (pueden ser 100MB+)
MAX_RETRIES  = 3
BACKOFF_BASE = 1.8
CHUNK_SIZE   = 65536   # 64 KB por chunk

con = Console()


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MediaItem:
    hash         : str
    ext          : str
    b0           : str = field(init=False)
    b1           : str = field(init=False)
    url_fullres  : Optional[str] = None   # img.kemono.cr/data/... o n1-n4 (derivado)
    url_thumb    : Optional[str] = None   # img.kemono.cr/thumbnail/... (siempre vivo)
    best_url     : Optional[str] = None   # URL elegida para descarga
    is_video     : bool = False
    is_thumb     : bool = False           # True si solo se pudo descargar thumbnail
    size_bytes   : int  = 0
    mime         : str  = ""
    dl_path      : Optional[Path] = None
    status       : str  = "pending"       # pending/validated/thumb_fallback/done/failed
    dl_error     : Optional[str] = None

    def __post_init__(self):
        self.b0       = self.hash[:2].lower()
        self.b1       = self.hash[2:4].lower()
        self.is_video = self.ext.lower() in VIDEO_EXTS


@dataclass
class NetworkResult:
    host    : str
    port    : int = 443
    dns     : Optional[str] = None
    tcp     : Optional[str] = None
    tls     : Optional[str] = None
    http    : Optional[int] = None
    latency : float = 0.0
    error   : Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# UTILIDADES GLOBALES
# ══════════════════════════════════════════════════════════════════════════════

def parse_post_url(url: str) -> Tuple[str, str, str]:
    """Extrae (service, user_id, post_id) de URL Kemono/Coomer."""
    m = re.search(r"(?:kemono|coomer)\.\w+/(\w+)/user/(\w+)/post/(\w+)", url)
    return (m.group(1), m.group(2), m.group(3)) if m else ("x", "0", "0")


def extract_hash(url: str) -> Optional[str]:
    m = HASH64_RE.search(urlparse(url).path)
    return m.group(0).lower() if m else None


def is_media_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    path = urlparse(url).path.lower()
    ext  = Path(path).suffix
    return (ext in MEDIA_EXTS
            or "/data/"      in path
            or "/thumbnail/" in path
            or "/attachments/" in path)


def validate_magic(data: bytes, ext: str) -> bool:
    """Verifica magic bytes del archivo descargado."""
    magic = MAGIC.get(ext.lower(), b"")
    if not magic:
        return len(data) > 256
    return data[:len(magic)] == magic


def walk_json(obj, found: Set[str], base: str = "https://kemono.cr", depth: int = 0):
    """Extrae recursivamente URLs de media de un objeto JSON."""
    if depth > 14:
        return
    if isinstance(obj, str):
        if obj.startswith("/data/") or obj.startswith("/thumbnail/"):
            found.add(f"{base}{obj}")
        elif obj.startswith("http") and is_media_url(obj):
            found.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            walk_json(v, found, base, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            walk_json(item, found, base, depth + 1)


# ══════════════════════════════════════════════════════════════════════════════
# FASE 1 — NETWORK ANALYZER
# ══════════════════════════════════════════════════════════════════════════════

class NetworkAnalyzer:
    """
    Análisis forense de conectividad:
    DNS → TCP SYN → TLS handshake → HTTP HEAD → diagnóstico
    Detecta: timeout DROP, REJECT, DNS fail, geoblock, CDN caído.
    """

    def __init__(self):
        self.results   : List[NetworkResult] = []
        self._dead     : Set[str] = set(DEAD_NODES)

    def is_dead(self, host: str) -> bool:
        return host in self._dead

    def mark_dead(self, host: str):
        self._dead.add(host)

    def timeout_for(self, host: str) -> float:
        return TIMEOUT_DEAD if self.is_dead(host) else TIMEOUT_FAST

    def probe_host(self, host: str, port: int = 443, path: str = "/") -> NetworkResult:
        r = NetworkResult(host=host, port=port)

        # ── DNS ──────────────────────────────────────────────────────────────
        try:
            r.dns = socket.gethostbyname(host)
        except socket.gaierror as e:
            r.error = f"DNS FAIL: {e}"
            self.mark_dead(host)
            return r

        # ── TCP SYN ───────────────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=4.0) as sock:
                r.tcp = "OK"

                # ── TLS handshake ─────────────────────────────────────────────
                try:
                    ctx = ssl.create_default_context()
                    with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                        r.tls = tls_sock.version()

                        # ── HTTP HEAD ─────────────────────────────────────────
                        try:
                            conn = http.client.HTTPSConnection(
                                host, port, timeout=4.0,
                                context=ssl.create_default_context()
                            )
                            conn.request("HEAD", path,
                                         headers={"Host": host, "User-Agent": UA,
                                                  "Accept": "*/*"})
                            resp   = conn.getresponse()
                            r.http = resp.status
                            conn.close()
                        except Exception:
                            r.http = None
                except ssl.SSLError as e:
                    r.tls  = f"SSL ERR"
                    r.error = str(e)[:60]

                r.latency = (time.monotonic() - t0) * 1000

        except (socket.timeout, TimeoutError):
            r.tcp   = "SYN TIMEOUT (DROP)"
            r.error = "TCP DROP — firewall o servidor offline"
            self.mark_dead(host)
        except ConnectionRefusedError:
            r.tcp   = "REFUSED"
            r.error = "Puerto rechazado"
            self.mark_dead(host)
        except Exception as e:
            r.tcp   = f"ERROR"
            r.error = f"{type(e).__name__}: {str(e)[:50]}"

        return r

    def run_analysis(self, extra_hosts: Optional[List[str]] = None) -> List[NetworkResult]:
        hosts = list(dict.fromkeys(MIRROR_HOSTS + (extra_hosts or [])))

        con.rule("[bold cyan]FASE 1 — Análisis de Red[/bold cyan]")
        tbl = Table(header_style="bold magenta", show_lines=False, expand=False)
        tbl.add_column("Host",        style="cyan", width=26)
        tbl.add_column("IP",                        width=16)
        tbl.add_column("TCP",                       width=20)
        tbl.add_column("HTTP",                      width=6)
        tbl.add_column("ms",                        width=7)
        tbl.add_column("Diagnóstico", style="dim",  width=40)

        for host in hosts:
            r = self.probe_host(host)
            self.results.append(r)

            ip_s   = r.dns  or "[red]DNS FAIL[/red]"
            tcp_c  = "green" if r.tcp == "OK" else "red"
            http_c = ("green"  if r.http and r.http < 400 else
                      "yellow" if r.http else "dim")
            tbl.add_row(
                host,
                ip_s,
                f"[{tcp_c}]{r.tcp or '—'}[/{tcp_c}]",
                f"[{http_c}]{r.http or '—'}[/{http_c}]",
                f"{r.latency:.0f}" if r.latency else "—",
                (r.error or "OK")[:40],
            )

        con.print(tbl)
        alive = sum(1 for r in self.results if r.tcp == "OK")
        con.print(f"  Hosts vivos: [green]{alive}[/green] / {len(self.results)}\n")
        return self.results


# ══════════════════════════════════════════════════════════════════════════════
# FASE 2 — PLAYWRIGHT EXTRACTOR
# ══════════════════════════════════════════════════════════════════════════════

class PlaywrightExtractor:
    """
    Extracción forense con Playwright Chromium:
    - Intercepción de TODA la red (request/response)
    - HAR log
    - DOM: img, a, source, srcset, data-src, data-full, background-image
    - __NUXT__ / hydration payloads / inline <script> JSON
    - API fetch desde contexto del navegador (con cookies de sesión)
    """

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.net_reqs  : List[dict] = []
        self.json_blobs: List[dict] = []
        self.seen      : Set[str]   = set()

    async def extract(self, post_url: str) -> Tuple[List[str], dict, str]:
        """Retorna (urls_únicas, dom_data, post_title)."""
        if not _PLAYWRIGHT:
            con.print("[red][ERROR] Playwright no instalado.[/red]")
            con.print("  pip install playwright && python -m playwright install chromium")
            return [], {}, ""

        service, uid, pid = parse_post_url(post_url)
        base_host = urlparse(post_url).netloc  # kemono.cr o coomer.cr

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            ctx = await browser.new_context(
                user_agent=UA,
                viewport={"width": 1920, "height": 1080},
            )

            page = await ctx.new_page()

            # ── Interceptores de red ──────────────────────────────────────────
            def on_req(req):
                self.net_reqs.append({"url": req.url, "type": req.resource_type})
                if is_media_url(req.url):
                    self.seen.add(req.url)

            async def on_resp(resp):
                url = resp.url
                ct  = resp.headers.get("content-type", "")
                if is_media_url(url):
                    self.seen.add(url)
                # Capturar JSON de la API y del CDN de anuncios es ruido — filtrar
                if ("application/json" in ct
                        and ("kemono" in url or "coomer" in url or "/api/" in url)):
                    try:
                        body = await resp.text()
                        if len(body) > 10:
                            self.json_blobs.append({"url": url, "body": body[:80000]})
                    except Exception:
                        pass

            page.on("request",  on_req)
            page.on("response", on_resp)

            # ── Navegar ───────────────────────────────────────────────────────
            con.log(f"[cyan][NAV] {post_url}[/cyan]")
            try:
                await page.goto(post_url, wait_until="domcontentloaded", timeout=40000)
                title = await page.title()
                con.log(f"[green][OK] DOM cargado | {title[:70]}[/green]")
            except Exception as e:
                con.log(f"[red][FAIL] Navegación: {e}[/red]")
                await browser.close()
                return [], {}, ""

            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await asyncio.sleep(1.5)

            # ── Scroll para lazy loading ──────────────────────────────────────
            try:
                total_h = await page.evaluate("document.body.scrollHeight")
                steps   = 14
                for i in range(steps):
                    await page.evaluate(f"window.scrollTo(0, {int(total_h * (i+1) / steps)})")
                    await asyncio.sleep(0.15)
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1.5)
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            # ── Extracción DOM — todo en JavaScript para evitar SyntaxWarning ─
            # Raw string para que Python no interprete \s, \S, \w, etc.
            dom = await page.evaluate(r"""
            () => {
                const media = new Set();
                const attachLinks = [];
                const extPat = /[.](jpg|jpeg|png|gif|webp|avif|mp4|webm|zip|rar|mov)([?#]|$)/i;

                // ── img tags ─────────────────────────────────────────────────
                document.querySelectorAll('img').forEach(img => {
                    if (img.src && !img.src.startsWith('data:')) media.add(img.src);
                    for (const attr of ['data-src','data-lazy','data-original',
                                        'data-full','data-lazy-src','data-zoom-src']) {
                        const v = img.getAttribute(attr);
                        if (v && !v.startsWith('data:')) media.add(v);
                    }
                    if (img.srcset) img.srcset.split(',').forEach(s => {
                        const u = (s.trim().split(/\s+/)[0] || '').trim();
                        if (u && !u.startsWith('data:')) media.add(u);
                    });
                });

                // ── a[href] con media / kemono paths ─────────────────────────
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.href || '';
                    if (!h || h.startsWith('javascript')) return;
                    if (h.includes('/data/') || h.includes('/thumbnail/') ||
                        h.includes('attachment') || extPat.test(h)) {
                        media.add(h);
                        attachLinks.push({
                            href: h,
                            text: (a.textContent || '').trim().slice(0, 100)
                        });
                    }
                });

                // ── video / source ────────────────────────────────────────────
                document.querySelectorAll('video, source, track').forEach(el => {
                    if (el.src  && !el.src.startsWith('data:'))  media.add(el.src);
                    if (el.srcset) el.srcset.split(',').forEach(s => {
                        const u = (s.trim().split(/\s+/)[0] || '').trim();
                        if (u) media.add(u);
                    });
                    const ds = el.getAttribute('data-src') || el.getAttribute('data-video');
                    if (ds && !ds.startsWith('data:')) media.add(ds);
                });

                // ── background-image en style ─────────────────────────────────
                document.querySelectorAll('[style]').forEach(el => {
                    const bg = el.style.backgroundImage || '';
                    if (!bg || bg === 'none') return;
                    (bg.match(/url\(["']?([^"')]+)["']?\)/g) || []).forEach(m => {
                        const v = m.replace(/^url\(["']?/, '').replace(/["']?\)$/, '');
                        if (v && !v.startsWith('data:')) media.add(v);
                    });
                });

                // ── __NUXT__ y otros stores de hidratación ────────────────────
                const hydration = {};
                for (const k of ['__NUXT__','__INITIAL_STATE__','__APP_STATE__',
                                  '__PRELOADED_STATE__','__DATA__','__POST__',
                                  '__KEMONO__', 'initialState']) {
                    if (typeof window[k] !== 'undefined') {
                        try { hydration[k] = JSON.parse(JSON.stringify(window[k])); }
                        catch(e) {}
                    }
                }

                // ── inline <script> con rutas de media ────────────────────────
                const scriptData = [];
                document.querySelectorAll('script:not([src])').forEach(s => {
                    const t = s.textContent || '';
                    if (!t.trim() || t.length > 600000) return;
                    const relevant = t.includes('/data/') || t.includes('attachment') ||
                                     t.includes('file_name') || t.includes('__NUXT__') ||
                                     t.includes('"path"') || t.includes('thumbnail');
                    if (!relevant) return;
                    const m = t.match(/(\{[\s\S]{10,}\}|\[[\s\S]{10,}\])/);
                    if (m) {
                        try { scriptData.push(JSON.parse(m[0])); return; } catch(e) {}
                    }
                    if (t.length < 60000) scriptData.push({ _raw: t.slice(0, 10000) });
                });

                // ── preload / prefetch links ──────────────────────────────────
                const preloads = [];
                document.querySelectorAll('link[rel="preload"],link[rel="prefetch"]')
                    .forEach(l => { if (l.href && !l.href.startsWith('data:')) preloads.push(l.href); });

                // ── título y contenido del post ───────────────────────────────
                const titleEl   = document.querySelector(
                    '.post__title h1, .post__title, h1.page__title, h1');
                const contentEl = document.querySelector(
                    '.post__content, [class*="post-content"], [class*="content__body"]');

                return {
                    media:            [...media],
                    attachment_links: attachLinks,
                    hydration:        hydration,
                    script_data:      scriptData,
                    preloads:         preloads,
                    post_title:       titleEl   ? titleEl.textContent.trim()        : null,
                    content_preview:  contentEl ? contentEl.innerText.slice(0, 400) : null,
                    page_title:       document.title,
                };
            }
            """)

            # ── API calls desde el navegador (incluye cookies de sesión) ──────
            api_endpoints = [
                f"https://{base_host}/api/v1/{service}/user/{uid}/post/{pid}",
                f"https://kemono.cr/api/v1/{service}/user/{uid}/post/{pid}",
            ]
            for ep in api_endpoints:
                ep_js = json.dumps(ep)
                res = await page.evaluate(f"""
                async () => {{
                    try {{
                        const r = await fetch({ep_js}, {{
                            credentials: 'include',
                            headers: {{
                                'Accept': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest'
                            }}
                        }});
                        const t = await r.text();
                        return {{ status: r.status, body: t.slice(0, 80000) }};
                    }} catch(e) {{ return {{ error: String(e) }}; }}
                }}
                """)
                st = res.get("status")
                if st == 200:
                    self.json_blobs.append({
                        "url": ep, "body": res["body"], "src": "browser_fetch"
                    })
                    con.log(f"[green][API] 200 OK → {ep}[/green]")
                else:
                    con.log(f"[yellow][API] {st or 'ERR'} → {ep}[/yellow]")

            await ctx.close()
            await browser.close()

        # ── Compilar todas las URLs únicas ────────────────────────────────────
        base_url = f"https://{base_host}"
        all_urls: Set[str] = set(self.seen)

        for u in dom.get("media", []):
            if u and u.startswith("http") and is_media_url(u):
                all_urls.add(u)
        for al in dom.get("attachment_links", []):
            u = al.get("href", "")
            if u and u.startswith("http"):
                all_urls.add(u)
        for u in dom.get("preloads", []):
            if u and is_media_url(u):
                all_urls.add(u)

        # JSON blobs: API responses + inline scripts
        for blob in self.json_blobs:
            body = blob.get("body", "")
            try:
                data = json.loads(body)
                walk_json(data, all_urls, base_url)
            except Exception:
                for u in re.findall(r'"(https?://[^"]{10,})"', body):
                    if is_media_url(u):
                        all_urls.add(u)
                for p in re.findall(r'"(/(?:data|thumbnail)/[^"]{8,})"', body):
                    all_urls.add(f"{base_url}{p}")

        # __NUXT__ / hydration
        for val in dom.get("hydration", {}).values():
            if isinstance(val, (dict, list)):
                walk_json(val, all_urls, base_url)

        # Inline script data
        for sd in dom.get("script_data", []):
            if isinstance(sd, (dict, list)):
                walk_json(sd, all_urls, base_url)
            elif isinstance(sd, dict) and "_raw" in sd:
                raw = sd["_raw"]
                for p in re.findall(r'["\']?(/data/[^"\'>\s]{10,})["\']?', raw):
                    all_urls.add(f"{base_url}{p}")

        post_title = dom.get("post_title") or dom.get("page_title") or ""
        con.log(
            f"[green][EXTRACT] {len(all_urls)} URLs únicas  |  "
            f"\"{post_title[:60]}\"[/green]"
        )
        return sorted(all_urls), dom, post_title


# ══════════════════════════════════════════════════════════════════════════════
# FASE 3 — URL RESOLVER
# ══════════════════════════════════════════════════════════════════════════════

class UrlResolver:
    """
    Clasifica URLs, construye MediaItems y valida variantes full-res vs thumbnail.

    CLASIFICACIÓN CORRECTA (crítica para evitar el bug de v1):
      img.kemono.cr/thumbnail/data/hash.ext  → thumbnail (CDN viva)
      img.kemono.cr/data/hash.ext            → posible full-res (misma CDN)
      n1-n4.kemono.cr/data/hash.ext          → storage offline
      kemono.cr/data/hash.ext                → 302→HTML (no sirve archivos)
    """

    def __init__(self, net: NetworkAnalyzer):
        self.net = net

    def classify(self, urls: List[str]) -> Tuple[List[str], List[str], List[str], List[str]]:
        """Clasifica en (storage_nodes, img_cdn_full, thumbnails, otros)."""
        storage_full, img_cdn, thumbs, others = [], [], [], []
        for url in urls:
            host = urlparse(url).hostname or ""
            path = urlparse(url).path.lower()
            if "/thumbnail/data/" in path:
                thumbs.append(url)
            elif host in {f"n{i}.kemono.cr" for i in range(1, 7)}:
                storage_full.append(url)
            elif host == IMG_CDN and "/data/" in path:
                img_cdn.append(url)
            elif is_media_url(url) and HASH64_RE.search(path):
                others.append(url)
            else:
                others.append(url)
        return storage_full, img_cdn, thumbs, others

    def build_items(
        self,
        storage_full: List[str],
        img_cdn     : List[str],
        thumbs      : List[str],
    ) -> List[MediaItem]:
        """Construye MediaItems únicos por hash SHA-256."""
        items: Dict[str, MediaItem] = {}

        def _add(url: str, is_thumb_src: bool):
            h = extract_hash(url)
            if not h:
                return
            ext = Path(urlparse(url).path).suffix.lower() or ".jpg"
            if h not in items:
                items[h] = MediaItem(hash=h, ext=ext)
            item = items[h]

            if is_thumb_src:
                item.url_thumb = url
                # Derivar full-res en la misma CDN quitando /thumbnail/
                derived = url.replace("/thumbnail/data/", "/data/")
                if not item.url_fullres:
                    item.url_fullres = derived
            else:
                host = urlparse(url).hostname or ""
                if not item.url_fullres:
                    item.url_fullres = url
                elif not self.net.is_dead(host):
                    # Preferir hosts vivos como url_fullres
                    item.url_fullres = url

        for u in thumbs:
            _add(u, True)
        for u in img_cdn:
            _add(u, False)
        for u in storage_full:
            _add(u, False)

        return list(items.values())

    async def validate_all(
        self, items: List[MediaItem], session: aiohttp.ClientSession
    ) -> None:
        """
        HEAD o GET concurrente para cada variante.
        Muchos servidores bloquean HEAD → fallback a GET Range:bytes=0-0.
        """
        sem = asyncio.Semaphore(CONCURRENCY * 3)  # HEAD es barato

        async def _probe(item: MediaItem):
            async with sem:
                # 1) Intentar full-res
                if item.url_fullres:
                    host = urlparse(item.url_fullres).hostname or ""
                    if not self.net.is_dead(host):
                        r = await self._probe_url(item.url_fullres, session)
                        if r and r[0] in (200, 206):
                            item.best_url   = item.url_fullres
                            item.size_bytes = r[1]
                            item.mime       = r[2]
                            item.is_thumb   = False
                            item.status     = "validated"
                            return

                # 2) Fallback a thumbnail (img.kemono.cr — siempre vivo)
                if item.url_thumb:
                    r = await self._probe_url(item.url_thumb, session)
                    if r and r[0] in (200, 206):
                        item.best_url   = item.url_thumb
                        item.size_bytes = r[1]
                        item.mime       = r[2]
                        item.is_thumb   = True
                        item.status     = "thumb_fallback"
                        return

                item.status = "no_url"

        await asyncio.gather(*[_probe(i) for i in items])

    async def _probe_url(
        self, url: str, session: aiohttp.ClientSession
    ) -> Optional[Tuple[int, int, str]]:
        host    = urlparse(url).hostname or ""
        timeout = aiohttp.ClientTimeout(
            total=self.net.timeout_for(host),
            sock_read=self.net.timeout_for(host),
        )
        hdrs = {"User-Agent": UA, "Referer": "https://kemono.cr/"}

        # Intentar HEAD
        try:
            async with session.head(
                url, timeout=timeout, allow_redirects=True, headers=hdrs
            ) as r:
                if r.status == 405:
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=405)
                cl = int(r.headers.get("content-length", 0))
                ct = r.headers.get("content-type", "")
                return (r.status, cl, ct)
        except aiohttp.ClientResponseError as e:
            if e.status != 405:
                return None
        except (aiohttp.ServerTimeoutError, asyncio.TimeoutError):
            self.net.mark_dead(host)
            return None
        except Exception:
            return None

        # HEAD bloqueado → GET Range (solo primeros 512 bytes)
        try:
            async with session.get(
                url, timeout=timeout, allow_redirects=True,
                headers={**hdrs, "Range": "bytes=0-511"},
            ) as r:
                raw_cl  = r.headers.get("content-range", "").split("/")
                cl      = int(raw_cl[-1]) if raw_cl[-1].isdigit() else \
                          int(r.headers.get("content-length", 0))
                ct      = r.headers.get("content-type", "")
                return (r.status, cl, ct)
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════════════════════
# FASE 4 — DOWNLOADER
# ══════════════════════════════════════════════════════════════════════════════

class Downloader:
    """
    Descargador asíncrono profesional:
    - Resume parcial (Range: bytes=N-)
    - Retries con backoff exponencial
    - Fallback full-res → thumbnail automático
    - Validación magic bytes
    - Soporte vídeos grandes (timeout extendido, chunks grandes)
    - Logs forenses: [DL] [THUMB] [VIDEO] [RETRY] [TIMEOUT] [FALLBACK] [FAIL]
    """

    def __init__(self, img_dir: Path, net: NetworkAnalyzer, concurrency: int, post_id: str = ""):
        self.img_dir = img_dir
        self.net     = net
        self.sem     = asyncio.Semaphore(concurrency)
        self.post_id = post_id

    async def download_all(
        self, items: List[MediaItem], session: aiohttp.ClientSession
    ) -> None:
        ready = [i for i in items if i.best_url or i.url_thumb]
        if not ready:
            con.print("[red][WARN] Sin items descargables.[/red]")
            return

        # Asignar best_url a items que no pasaron validación pero tienen thumbnail
        for item in ready:
            if not item.best_url and item.url_thumb:
                item.best_url = item.url_thumb
                item.is_thumb = True
                item.status   = "thumb_fallback"

        total_est = sum(max(i.size_bytes, 50_000) for i in ready)

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=con,
        ) as prog:
            task = prog.add_task(
                f"[cyan]Descargando {len(ready)} archivos", total=total_est
            )
            await asyncio.gather(*[
                self._dl(idx, item, session, prog, task)
                for idx, item in enumerate(ready)
            ])

    async def _dl(
        self,
        idx    : int,
        item   : MediaItem,
        session: aiohttp.ClientSession,
        prog,
        task,
    ) -> None:
        prefix = f"{self.post_id}-" if self.post_id else ""
        fname = f"{prefix}{idx + 1}{item.ext}"
        fpath = self.img_dir / fname
        await self._download_one(item, fpath, session, prog, task, fname)

    async def _download_one(
        self,
        item   : MediaItem,
        fpath  : Path,
        session: aiohttp.ClientSession,
        prog,
        task,
        fname  : str,
    ) -> None:
        url = item.best_url or item.url_thumb
        if not url:
            item.status = "failed"
            return

        # Skip si el archivo ya existe con tamaño correcto
        if (fpath.exists() and item.size_bytes > 0
                and fpath.stat().st_size >= item.size_bytes * 0.98):
            prog.advance(task, max(item.size_bytes, 50_000))
            con.log(f"[dim][SKIP] {fname} (ya existe)[/dim]")
            item.dl_path = fpath
            item.status  = "done"
            return

        timeout_s = TIMEOUT_VID if item.is_video else TIMEOUT_DL

        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                wait = BACKOFF_BASE ** attempt
                con.log(
                    f"[yellow][RETRY {attempt}/{MAX_RETRIES}] {fname} "
                    f"en {wait:.1f}s[/yellow]"
                )
                await asyncio.sleep(wait)

            host    = urlparse(url).hostname or ""
            t_total = TIMEOUT_DEAD if self.net.is_dead(host) else timeout_s
            timeout = aiohttp.ClientTimeout(total=t_total, sock_read=t_total)

            resume_pos = fpath.stat().st_size if fpath.exists() else 0
            hdrs: Dict[str, str] = {
                "User-Agent": UA,
                "Referer":    "https://kemono.cr/",
                "Accept":     "image/webp,image/avif,image/*,video/*,*/*;q=0.8",
            }
            if resume_pos > 0:
                hdrs["Range"] = f"bytes={resume_pos}-"

            try:
                async with self.sem:
                    con.log(f"[cyan][REQ] {fname} ← {url[:70]}[/cyan]")
                    async with session.get(
                        url, timeout=timeout, headers=hdrs, allow_redirects=True
                    ) as resp:
                        # 416 Range Not Satisfiable → borrar y reintentar sin Range
                        if resp.status == 416:
                            fpath.unlink(missing_ok=True)
                            resume_pos = 0
                            hdrs.pop("Range", None)
                            async with session.get(
                                url, timeout=timeout, headers=hdrs, allow_redirects=True
                            ) as resp2:
                                resp = resp2

                        # Falló → intentar fallback a thumbnail
                        if resp.status not in (200, 206):
                            con.log(
                                f"[yellow][HTTP {resp.status}] {fname} "
                                f"→ intentando fallback[/yellow]"
                            )
                            if (not item.is_thumb
                                    and item.url_thumb
                                    and url != item.url_thumb):
                                url           = item.url_thumb
                                item.is_thumb = True
                                con.log(f"[yellow][FALLBACK→THUMB] {fname}[/yellow]")
                                continue
                            item.dl_error = f"HTTP {resp.status}"
                            break

                        # ── Descargar chunks ──────────────────────────────────
                        mode       = "ab" if resp.status == 206 else "wb"
                        downloaded = 0
                        first_chunk: Optional[bytes] = None

                        async with aiofiles.open(fpath, mode) as f:
                            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                if first_chunk is None:
                                    first_chunk = chunk
                                await f.write(chunk)
                                downloaded += len(chunk)
                                prog.advance(task, len(chunk))

                        total = fpath.stat().st_size if fpath.exists() else downloaded

                        if total < 256:
                            fpath.unlink(missing_ok=True)
                            item.dl_error = "vacío"
                            continue

                        # Validar magic bytes
                        if first_chunk and not validate_magic(first_chunk, item.ext):
                            con.log(
                                f"[yellow][WARN] {fname}: magic bytes "
                                f"inesperados (puede ser WebP servido como .jpg)[/yellow]"
                            )

                        item.dl_path    = fpath
                        item.size_bytes = total
                        item.status     = "done"

                        size_s = (f"{total // 1048576}MB" if total > 1048576
                                  else f"{total // 1024}KB")
                        type_s = ("VIDEO"     if item.is_video
                                  else "THUMB" if item.is_thumb
                                  else "FULLRES")
                        tag = "[VIDEO]" if item.is_video else "[THUMB]" if item.is_thumb else "[DL]"
                        con.log(
                            f"[green]{tag} {fname}  {size_s}  {type_s}[/green]"
                        )
                        return

            except (aiohttp.ServerTimeoutError, asyncio.TimeoutError):
                self.net.mark_dead(host)
                item.dl_error = f"timeout:{host}"
                con.log(f"[yellow][TIMEOUT] {fname} host={host}[/yellow]")

                # Host muerto → fallback inmediato a thumbnail
                if (self.net.is_dead(host)
                        and item.url_thumb
                        and url != item.url_thumb):
                    url           = item.url_thumb
                    item.is_thumb = True
                    con.log(f"[yellow][FALLBACK→THUMB] {fname} (host muerto)[/yellow]")
                    attempt = -1  # reiniciar conteo
                    continue

            except aiohttp.ClientError as e:
                item.dl_error = f"{type(e).__name__}: {str(e)[:50]}"
                con.log(f"[red][ERR] {fname}: {e}[/red]")

        if item.status != "done":
            item.status = "failed"
            con.log(f"[red][FAIL] {fname}: {item.dl_error}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
# FASE 5 — EXPORTER
# ══════════════════════════════════════════════════════════════════════════════

class Exporter:
    """Exporta metadata.json, images_urls.txt, fullres_urls.txt, failed.txt."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def export(
        self,
        items      : List[MediaItem],
        post_url   : str,
        post_title : str,
        dom_data   : dict,
        net_results: List[NetworkResult],
    ) -> None:
        service, uid, pid = parse_post_url(post_url)

        done   = [i for i in items if i.status == "done"]
        failed = [i for i in items if i.status == "failed"]

        total_bytes = sum(
            i.dl_path.stat().st_size
            for i in done if i.dl_path and i.dl_path.exists()
        )

        metadata = {
            "tool":        f"kemono_galeria.py v{VERSION}",
            "generated":   datetime.now().isoformat(),
            "post_url":    post_url,
            "post_title":  post_title,
            "service":     service,
            "user_id":     uid,
            "post_id":     pid,
            "network": [
                {
                    "host":    r.host,
                    "ip":      r.dns,
                    "tcp":     r.tcp,
                    "http":    r.http,
                    "latency": round(r.latency, 1),
                    "status":  "alive" if r.tcp == "OK" else "dead",
                }
                for r in net_results
            ],
            "summary": {
                "total":      len(items),
                "fullres":    sum(1 for i in done if not i.is_thumb and not i.is_video),
                "thumbnails": sum(1 for i in done if i.is_thumb),
                "videos":     sum(1 for i in done if i.is_video),
                "failed":     len(failed),
                "total_mb":   round(total_bytes / 1048576, 2),
            },
            "items": [
                {
                    "hash":       i.hash,
                    "ext":        i.ext,
                    "status":     i.status,
                    "is_thumb":   i.is_thumb,
                    "is_video":   i.is_video,
                    "best_url":   i.best_url,
                    "url_fullres": i.url_fullres,
                    "url_thumb":  i.url_thumb,
                    "size_bytes": i.size_bytes,
                    "mime":       i.mime,
                    "file":       i.dl_path.name if i.dl_path else None,
                    "error":      i.dl_error,
                }
                for i in items
            ],
        }

        cache_dir = self.output_dir / "_cache"
        cache_dir.mkdir(exist_ok=True)

        with open(cache_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        with open(cache_dir / "fullres_urls.txt", "w", encoding="utf-8") as f:
            for i in items:
                if i.url_fullres:
                    f.write(f"{i.url_fullres}\n")

        if failed:
            with open(self.output_dir / "failed.txt", "w", encoding="utf-8") as f:
                for i in failed:
                    f.write(f"{i.hash}\t{i.dl_error or ''}\t{i.url_fullres or ''}\n")

        exported = ["_cache/metadata.json", "_cache/fullres_urls.txt"]
        if failed:
            exported.append(f"failed.txt ({len(failed)} errores)")
        con.print(f"[green][OK] {', '.join(exported)}[/green]")


# ══════════════════════════════════════════════════════════════════════════════
# FASE 6 — ZIP BUILDER
# ══════════════════════════════════════════════════════════════════════════════

class ZipBuilder:
    """Empaqueta los archivos descargados en un ZIP con manifest.json."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    @staticmethod
    def _safe_filename(name: str) -> str:
        """Convierte un nombre personalizado en nombre de archivo válido."""
        # Reemplazar caracteres no permitidos en nombres de archivo de Windows/Linux
        for ch in r'\/:*?"<>|':
            name = name.replace(ch, "")
        # Doble barra → reemplazar con guión (ej. "A // B" → "A - B")
        name = re.sub(r"\s*//\s*", " - ", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name or "output"

    def build(
        self,
        items      : List[MediaItem],
        post_url   : str,
        post_title : str,
        zip_name   : Optional[str] = None,
    ) -> Optional[Path]:
        done = [
            i for i in items
            if i.status == "done" and i.dl_path and i.dl_path.exists()
        ]
        if not done:
            con.print("[yellow][WARN] Sin archivos para el ZIP.[/yellow]")
            return None

        service, uid, pid = parse_post_url(post_url)
        if zip_name:
            safe = self._safe_filename(zip_name)
            if not safe.lower().endswith(".zip"):
                safe += ".zip"
            zip_path = self.output_dir / safe
        else:
            zip_path = self.output_dir / f"recovered_{service}_{uid}_{pid}.zip"

        is_partial = any(i.is_thumb for i in done)
        note = "ZIP parcial — thumbnails (storage offline)" if is_partial else "ZIP completo — full-res"

        manifest = {
            "source_url":   post_url,
            "post_title":   post_title,
            "recovered_at": datetime.now().isoformat(),
            "tool":         f"kemono_galeria.py v{VERSION}",
            "note":         note,
            "files":        [],
        }

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for idx, item in enumerate(sorted(done, key=lambda x: x.dl_path.name)):
                arcname = item.dl_path.name
                zf.write(item.dl_path, arcname)
                manifest["files"].append({
                    "name":       arcname,
                    "hash":       item.hash,
                    "size":       item.dl_path.stat().st_size,
                    "is_thumb":   item.is_thumb,
                    "is_video":   item.is_video,
                    "source_url": item.best_url,
                })
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        sz = zip_path.stat().st_size
        sz_s = f"{sz // 1048576}MB" if sz > 1048576 else f"{sz // 1024}KB"
        con.log(
            f"[bold green][ZIP] {zip_path.name}  "
            f"({sz_s}, {len(done)} archivos)[/bold green]"
        )
        return zip_path


# ══════════════════════════════════════════════════════════════════════════════
# FASE 7 — VEREDICTO
# ══════════════════════════════════════════════════════════════════════════════

VERDICTS = {
    "A": ("green",  "RECUPERACIÓN COMPLETA — imágenes/vídeos full-res descargados."),
    "B": ("yellow", "SOLO THUMBNAILS — storage n1-n4 offline. Miniaturas descargadas."),
    "C": ("yellow", "PARCIAL — URLs identificadas pero storage caído. Reintentar más tarde."),
    "D": ("red",    "SITIO COMPLETAMENTE OFFLINE — imposible recuperar ahora."),
    "E": ("red",    "RECUPERACIÓN IMPOSIBLE — sin media detectada. Verifica la URL."),
}


def print_verdict(
    items      : List[MediaItem],
    net_results: List[NetworkResult],
    post_title : str,
    zip_path   : Optional[Path],
    output_dir : Path,
) -> str:
    done     = [i for i in items if i.status == "done"]
    fullres  = sum(1 for i in done if not i.is_thumb and not i.is_video)
    thumbs   = sum(1 for i in done if i.is_thumb)
    videos   = sum(1 for i in done if i.is_video)
    failed   = sum(1 for i in items if i.status == "failed")
    total_mb = round(
        sum(i.dl_path.stat().st_size for i in done if i.dl_path and i.dl_path.exists())
        / 1048576, 2
    )

    frontend_alive = any(
        r.tcp == "OK" and r.http and r.http < 400
        for r in net_results if r.host in ("kemono.cr", "coomer.cr", "www.kemono.cr")
    )
    storage_alive = any(
        r.tcp == "OK" for r in net_results
        if r.host in {f"n{i}.kemono.cr" for i in range(1, 5)}
    )
    cdn_alive = any(r.tcp == "OK" for r in net_results if r.host == IMG_CDN)

    def yn(v: bool) -> str:
        return "[green]SÍ[/green]" if v else "[red]NO[/red]"

    tbl = Table(header_style="bold magenta", show_lines=False, expand=False)
    tbl.add_column("Indicador",  style="cyan", width=38)
    tbl.add_column("Resultado",  width=32)

    tbl.add_row("Post título",             (post_title or "—")[:60])
    tbl.add_row("Frontend vivo",           yn(frontend_alive))
    tbl.add_row("Storage n1-n4 vivos",     yn(storage_alive))
    tbl.add_row("CDN imágenes (img.) viva",yn(cdn_alive))
    tbl.add_row("Imágenes full-res DL",    f"[green]{fullres}[/green]")
    tbl.add_row("Thumbnails (fallback)",   f"[yellow]{thumbs}[/yellow]")
    tbl.add_row("Vídeos descargados",      f"[cyan]{videos}[/cyan]")
    tbl.add_row("Fallidos",                f"[red]{failed}[/red]")
    tbl.add_row("Total descargado",        f"{total_mb} MB")
    tbl.add_row("ZIP reconstruido",        zip_path.name if zip_path else "—")
    con.print(tbl)

    # Determinar veredicto
    total_dl = fullres + thumbs + videos
    if fullres > 0 and failed == 0:
        v = "A"
    elif fullres > 0:
        v = "A"  # parcial full-res sigue siendo A
    elif thumbs > 0 and not storage_alive:
        v = "B"
    elif thumbs > 0:
        v = "B"
    elif not frontend_alive:
        v = "D"
    elif total_dl == 0 and len(items) > 0:
        v = "C"
    else:
        v = "E"

    color, desc = VERDICTS[v]
    con.print(Panel(
        f"[bold {color}]{v}) {desc}[/bold {color}]",
        title="[bold]Veredicto Final[/bold]",
        border_style=color,
    ))

    if v in ("B", "C"):
        con.print()
        con.print("[dim]Cuando los nodos n1-n4 vuelvan online, re-ejecuta:[/dim]")
        con.print(
            f"  [cyan]python kemono_galeria.py \"<URL>\" "
            f"--skip-playwright[/cyan]"
        )

    # Listar solo archivos relevantes para el usuario
    HIDDEN = {"_cache"}  # carpeta de archivos técnicos — ocultar del resumen
    con.print()
    con.print("[bold]Archivos generados:[/bold]")
    for fp in sorted(output_dir.rglob("*")):
        if not fp.is_file():
            continue
        if fp.name in HIDDEN:
            continue
        if fp.parent.name == "images":
            continue  # mostrar solo el folder, no cada imagen
        rel  = fp.relative_to(output_dir)
        sz   = fp.stat().st_size
        sz_s = f"{sz // 1048576}MB" if sz > 1048576 else f"{sz // 1024}KB"
        con.print(f"  [dim]{str(rel):<52}[/dim]  {sz_s:>8}")
    # Mostrar el folder images/ como resumen
    img_dir = output_dir / "images"
    if img_dir.exists():
        imgs   = list(img_dir.iterdir())
        total  = sum(f.stat().st_size for f in imgs if f.is_file())
        sz_s   = f"{total // 1048576}MB" if total > 1048576 else f"{total // 1024}KB"
        con.print(f"  [dim]{'images/ (' + str(len(imgs)) + ' archivos)':<52}[/dim]  {sz_s:>8}")

    return v


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

async def _main(args: argparse.Namespace) -> None:
    post_url = args.url.strip()

    # Validar URL y extraer partes antes de crear carpetas
    service, uid, pid = parse_post_url(post_url)
    if uid == "0":
        con.rule(f"[bold]Kemono / Coomer Gallery Recovery  v{VERSION}[/bold]")
        con.print(
            "[red][ERROR] URL inválida.\n"
            "  Formato esperado: https://kemono.cr/SERVICE/user/UID/post/PID[/red]"
        )
        sys.exit(1)

    # Si el usuario no especificó --output, crear subcarpeta propia por post
    # para que cada descarga quede aislada: kemono_galeria_output/{service}_{pid}/
    if args.output == str(OUTPUT_DIR):
        output_dir = OUTPUT_DIR / f"{service}_{pid}"
    else:
        output_dir = Path(args.output)

    img_dir = output_dir / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(exist_ok=True)

    con.rule(f"[bold]Kemono / Coomer Gallery Recovery  v{VERSION}[/bold]")
    con.print(f"  URL         : [cyan]{post_url}[/cyan]")
    con.print(f"  Salida      : [cyan]{output_dir}[/cyan]")
    con.print(f"  Concurrencia: {args.concurrency}")
    con.print()

    # ── FASE 1: Análisis de red (caché 1h) ──────────────────────────────────────
    net = NetworkAnalyzer()
    net_cache_file = OUTPUT_DIR / "network_cache.json"
    _net_from_cache = False

    if not args.skip_network:
        # Reutilizar cache si tiene menos de NETWORK_CACHE_TTL segundos
        if net_cache_file.exists():
            try:
                with open(net_cache_file, encoding="utf-8") as _f:
                    _nc = json.load(_f)
                if time.time() - _nc.get("timestamp", 0) < NETWORK_CACHE_TTL:
                    net.results = [
                        NetworkResult(**{k: v for k, v in r.items() if k != "status"})
                        for r in _nc["results"]
                    ]
                    for r in net.results:
                        if r.tcp != "OK":
                            net.mark_dead(r.host)
                    _net_from_cache = True
                    con.rule("[bold cyan]FASE 1 — Análisis de Red[/bold cyan]")
                    alive = sum(1 for r in net.results if r.tcp == "OK")
                    con.print(f"[dim][CACHE] Análisis de red reutilizado (< 1h)  "
                              f"Hosts vivos: {alive}/{len(net.results)}[/dim]\n")
            except Exception:
                pass  # cache corrupta → re-analizar

        if not _net_from_cache:
            extra = [urlparse(post_url).netloc]
            net.run_analysis(extra_hosts=extra)
            # Guardar cache
            net_cache_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(net_cache_file, "w", encoding="utf-8") as _f:
                    json.dump({
                        "timestamp": time.time(),
                        "results": [
                            {"host": r.host, "port": r.port, "dns": r.dns,
                             "tcp": r.tcp, "tls": r.tls, "http": r.http,
                             "latency": r.latency, "error": r.error}
                            for r in net.results
                        ]
                    }, _f, ensure_ascii=False)
            except Exception:
                pass
    else:
        con.print("[dim][SKIP] Análisis de red (--skip-network)[/dim]\n")

    # ── FASE 2: Extracción Playwright ─────────────────────────────────────────
    all_urls   : List[str] = []
    dom_data   : dict      = {}
    post_title : str       = ""
    cache_dir  = output_dir / "_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "extracted_urls.json"

    con.rule("[bold cyan]FASE 2 — Extracción Playwright[/bold cyan]")

    # Si ya existe cache del post (mismo post = no re-extraer)
    if cache_file.exists():
        with open(cache_file, encoding="utf-8") as f:
            cached = json.load(f)
        all_urls   = cached.get("urls", [])
        post_title = cached.get("post_title", "")
        dom_data   = cached.get("dom", {})
        con.print(f"[dim][CACHE] Post ya extraído — {len(all_urls)} URLs reutilizadas.[/dim]")
    else:
        extractor = PlaywrightExtractor(output_dir)
        try:
            all_urls, dom_data, post_title = await extractor.extract(post_url)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"urls": all_urls, "post_title": post_title, "dom": dom_data},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            con.print(f"[red][ERROR] Playwright: {e}[/red]")
            traceback.print_exc()
            if not args.continue_on_error:
                sys.exit(1)

    # ── FASE 3: Clasificar → derivar full-res → validar ───────────────────────
    con.rule("[bold cyan]FASE 3 — Clasificación y validación[/bold cyan]")

    resolver                        = UrlResolver(net)
    storage_full, img_cdn, thumbs, _ = resolver.classify(all_urls)

    con.print(f"  Storage n1-n4 (offline prob.) : {len(storage_full):>4}")
    con.print(f"  img.kemono.cr full            : {len(img_cdn):>4}")
    con.print(f"  Thumbnails (vivos)            : {len(thumbs):>4}")

    items = resolver.build_items(storage_full, img_cdn, thumbs)
    con.print(f"  Items únicos por hash         : [bold]{len(items)}[/bold]\n")

    if not items:
        con.print("[red][ERROR] Sin media detectada. Verifica la URL.[/red]")
        sys.exit(1)

    connector = aiohttp.TCPConnector(
        limit=30, ttl_dns_cache=300, enable_cleanup_closed=True
    )
    async with aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": UA},
    ) as session:

        with con.status("[cyan]Validando variantes full-res vs thumbnails…[/cyan]"):
            await resolver.validate_all(items, session)

        validated = sum(1 for i in items if i.status == "validated")
        fallbacks = sum(1 for i in items if i.status == "thumb_fallback")
        no_url    = sum(1 for i in items if i.status == "no_url")
        con.print(f"  Full-res disponibles : [green]{validated}[/green]")
        con.print(f"  Thumbnail fallback   : [yellow]{fallbacks}[/yellow]")
        con.print(f"  Sin URL válida       : [red]{no_url}[/red]\n")

        # ── FASE 4: Descarga ──────────────────────────────────────────────────
        con.rule("[bold cyan]FASE 4 — Descarga[/bold cyan]")
        dl = Downloader(img_dir, net, concurrency=args.concurrency, post_id=pid)
        await dl.download_all(items, session)

    # ── FASE 5: Exportar ──────────────────────────────────────────────────────
    con.rule("[bold cyan]FASE 5 — Exportar[/bold cyan]")
    Exporter(output_dir).export(items, post_url, post_title, dom_data, net.results)

    # ── FASE 6: ZIP ───────────────────────────────────────────────────────────
    con.rule("[bold cyan]FASE 6 — ZIP[/bold cyan]")
    if args.no_zip:
        zip_path = None
        con.print("[dim][SKIP] ZIP omitido (--no-zip)[/dim]")
    else:
        zip_path = ZipBuilder(output_dir).build(items, post_url, post_title, args.zip_name)

    # ── FASE 7: Veredicto ─────────────────────────────────────────────────────
    con.rule("[bold]FASE 7 — Veredicto[/bold]")
    print_verdict(items, net.results, post_title, zip_path, output_dir)

    # ── Abrir carpeta images directamente (Windows) ───────────────────────────
    if sys.platform == "win32":
        try:
            os.startfile(str(img_dir.resolve()))
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kemono_galeria.py",
        description=f"Kemono/Coomer Gallery Recovery v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python kemono_galeria.py "https://kemono.cr/patreon/user/101779509/post/109629764"
  python kemono_galeria.py "<url>" --output ./mi_salida --concurrency 8
  python kemono_galeria.py "<url>" --zip-name "Zenith Greyrat [XTRAS] (Patreon)"
  python kemono_galeria.py "<url>" --zip-name "Zenith_Greyrat_xtrapack1_Deik0.zip"
  python kemono_galeria.py "<url>" --skip-playwright   # reutilizar cache
  python kemono_galeria.py "<url>" --skip-network      # omitir analisis TCP/DNS
        """,
    )
    parser.add_argument("url",
        help="URL del post (kemono.cr o coomer.cr)")
    parser.add_argument("--output",
        default=str(OUTPUT_DIR),
        help=f"Carpeta de salida (default: {OUTPUT_DIR})")
    parser.add_argument("--concurrency",
        type=int, default=CONCURRENCY,
        help=f"Descargas paralelas (default: {CONCURRENCY})")
    parser.add_argument("--skip-playwright",
        action="store_true",
        help="Usar cache de extracted_urls.json si existe")
    parser.add_argument("--skip-network",
        action="store_true",
        help="Omitir análisis TCP/DNS (más rápido)")
    parser.add_argument("--continue-on-error",
        action="store_true",
        help="Continuar aunque Playwright falle")
    parser.add_argument("--zip-name",
        default=None,
        metavar="NOMBRE",
        help=("Nombre personalizado del ZIP de salida. "
              "Ej: \"Zenith Greyrat [XTRAS] (Patreon)\" "
              "(caracteres especiales se sanean automáticamente)"))
    parser.add_argument("--no-zip",
        action="store_true",
        help="No crear archivo ZIP (conservar solo las imágenes)")

    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
