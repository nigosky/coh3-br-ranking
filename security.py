"""Primitivas centrais de segurança: TLS 1.2+, anti-SSRF, anti-bomb HTTP/Gzip, redaction de segredos e I/O atômico."""

from __future__ import annotations

import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

BRT_TZ = timezone(timedelta(hours=-3))
SAFE_WEBHOOK_PLACEHOLDER = "COLE_AQUI_VIA_ENV_DISCORD_RANKING_WEBHOOK_URL"

ALLOWED_DISCORD_HOSTS: frozenset[str] = frozenset(
    {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
)

WEBHOOK_PATH_RE = re.compile(r"^/api/(?:v\d+/)?webhooks/\d{15,25}/[A-Za-z0-9_-]{20,120}$")
WEBHOOK_REQUEST_PATH_RE = re.compile(
    r"^/api/(?:v\d+/)?webhooks/\d{15,25}/[A-Za-z0-9_-]{20,120}(?:/messages/\d{15,25})?$"
)

WEBHOOK_REDACT_RE = re.compile(
    r"https?://[^\s/\"'<>]+/api/(?:v\d+/)?webhooks/[^\s\"'<>]+",
    re.IGNORECASE,
)
ENV_SECRET_REDACT_RE = re.compile(
    r"((?:DISCORD_(?:RANKING_|DAILY_RECAP_|BATTLEGROUPS_)?WEBHOOK_URL|STEAM_(?:WEB_)?API_KEY)\s*=\s*)[^\s\"'<>]+",
    re.IGNORECASE,
)
STEAM_API_KEY_QUERY_REDACT_RE = re.compile(
    r"([?&]key=)[A-Za-z0-9]{16,64}\b",
    re.IGNORECASE,
)
GITHUB_TOKEN_REDACT_RE = re.compile(
    r"\b(?:gh[pos]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
)

# Remove caracteres de controle ASCII, zero-width e overrides bidirecionais Unicode (anti-spoofing visual RLO)
UNSAFE_TEXT_CHARS_RE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+")
URL_FORBIDDEN_CHARS_RE = re.compile(r"[\x00-\x20\x7f]")
URL_PATH_TRAVERSAL_RE = re.compile(r"(?:^|/)\.\.(?:/|$)|%2e%2e|%5c|\\", re.IGNORECASE)

CSV_DANGEROUS_INITIALS = ("=", "+", "-", "@", "|")
_HTTP_THREAD_LOCAL = threading.local()


class RateLimiter:
    """Limitador de taxa thread-safe simples (intervalo mínimo entre requisições HTTP)."""

    def __init__(self, min_interval_sec: float = 0.06, floor_sec: float = 0.01) -> None:
        self.min_interval_sec = max(float(floor_sec), float(min_interval_sec))
        self._next_req_ts = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now_m = time.monotonic()
            target = max(now_m, self._next_req_ts)
            self._next_req_ts = target + self.min_interval_sec
            delay = target - now_m
        if delay > 0:
            time.sleep(delay)


def redact_secrets(text: Any) -> str:
    """Remove qualquer URL/token de Webhook do Discord, chave Steam Web API ou token GitHub em logs e erros."""
    raw = str(text or "")
    if not raw:
        return ""
    scrubbed = WEBHOOK_REDACT_RE.sub("[WEBHOOK_REDACTED]", raw)
    scrubbed = ENV_SECRET_REDACT_RE.sub(r"\1[SECRET_REDACTED]", scrubbed)
    scrubbed = STEAM_API_KEY_QUERY_REDACT_RE.sub(r"\1[STEAM_KEY_REDACTED]", scrubbed)
    return GITHUB_TOKEN_REDACT_RE.sub("[TOKEN_REDACTED]", scrubbed)


class SecretRedactingFilter(logging.Filter):
    """Filtro global de logging que impede vazamento acidental de Webhooks/tokens em stdout ou Actions."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, (str, BaseException)) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, (str, BaseException)) else a
                    for a in record.args
                )
        return True


def configure_secure_logging(level: int = logging.INFO, datefmt: str = "%Y-%m-%d %H:%M:%S") -> None:
    """Configura o logger raiz e anexa (idempotentemente) o filtro anti-vazamento em todos os handlers."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt=datefmt,
        )
    redact_filter = SecretRedactingFilter()
    if not any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        root.addFilter(redact_filter)
    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(redact_filter)


def strip_unsafe_chars(raw: Any) -> str:
    """Substitui caracteres de controle e caracteres Unicode invisíveis/BiDi por espaço simples."""
    return UNSAFE_TEXT_CHARS_RE.sub(" ", str(raw or "")).strip()


def sanitize_csv_cell(val: Any) -> Any:
    """Previne CSV Formula Injection (CWE-1236) mesmo com espaços/quebras de linha à esquerda."""
    if not isinstance(val, str):
        return val
    cleaned = UNSAFE_TEXT_CHARS_RE.sub(" ", val.replace("\x00", ""))
    probe = cleaned.lstrip(" ")
    if cleaned.startswith(("\t", "\r", "\n")) or probe.startswith(CSV_DANGEROUS_INITIALS):
        return f"'{cleaned}"
    return cleaned


def create_hardened_ssl_context() -> ssl.SSLContext:
    """Cria contexto TLS 1.2+ com verificação obrigatória de certificado e hostname."""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, "OP_NO_COMPRESSION"):
        ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx


class StrictNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Bloqueia qualquer redirecionamento HTTP 3xx (proteção estrita contra SSRF e vazamento de headers)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Redirecionamento HTTP bloqueado por política de segurança",
            headers,
            fp,
        )


def build_no_redirect_https_opener() -> urllib.request.OpenerDirector:
    """Constrói OpenerDirector HTTPS seguro sem redirecionamentos, preservando suporte a proxy do ambiente."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=create_hardened_ssl_context()),
        StrictNoRedirectHandler(),
    )


def get_thread_no_redirect_https_opener() -> urllib.request.OpenerDirector:
    """Retorna OpenerDirector HTTPS seguro (sem redirecionamentos) reutilizado por thread."""
    opener: urllib.request.OpenerDirector | None = getattr(_HTTP_THREAD_LOCAL, "opener", None)
    if opener is None:
        opener = build_no_redirect_https_opener()
        _HTTP_THREAD_LOCAL.opener = opener
    return opener


def validate_https_endpoint(
    url: str,
    allowed_hosts: set[str] | frozenset[str],
    path_prefix: str = "/",
    path_regex: re.Pattern[str] | None = None,
) -> urllib.parse.ParseResult:
    """Valida esquema HTTPS, host em allowlist, porta 443, ausência de credenciais/traversal e caminho permitido."""
    clean = (url or "").strip()
    if not clean or URL_FORBIDDEN_CHARS_RE.search(clean):
        raise ValueError("URL inválida ou contendo caracteres de controle proibidos.")
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme != "https":
        raise ValueError("Apenas conexões HTTPS são permitidas.")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError(f"Host bloqueado pela allowlist de segurança: {host or '<vazio>'}")
    if parsed.username or parsed.password:
        raise ValueError("Credenciais embutidas na URL são bloqueadas por segurança.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Porta inválida na URL.") from exc
    if port not in (None, 443):
        raise ValueError(f"Porta não autorizada bloqueada: {port}")
    raw_path = parsed.path or ""
    if URL_PATH_TRAVERSAL_RE.search(raw_path):
        raise ValueError("Sequência de traversal bloqueada no caminho da URL.")
    if path_prefix and not raw_path.startswith(path_prefix):
        raise ValueError("Caminho da URL fora do escopo permitido.")
    if path_regex is not None and not path_regex.fullmatch(raw_path):
        raise ValueError("Formato do caminho da URL rejeitado pela validação estrita.")
    return parsed


def validate_discord_webhook_url(url: str, allow_message_subpath: bool = False) -> bool:
    """Valida estritamente URLs de Webhook do Discord (HTTPS, host oficial, porta 443 e token válido)."""
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return False
    pattern = WEBHOOK_REQUEST_PATH_RE if allow_message_subpath else WEBHOOK_PATH_RE
    try:
        validate_https_endpoint(raw, ALLOWED_DISCORD_HOSTS, path_prefix="/api/", path_regex=pattern)
        return True
    except ValueError:
        return False


def read_bounded_http_body(resp: Any, max_bytes: int) -> bytes:
    """
    Lê resposta HTTP com limite rígido de bytes no fio e na descompressão Gzip,
    prevenindo ataques de exaustão de memória / Gzip Bomb (CWE-409).
    """
    raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RuntimeError(f"Resposta HTTP excedeu o limite máximo seguro ({max_bytes} bytes).")

    encoding = ""
    if hasattr(resp, "headers") and resp.headers is not None:
        encoding = str(resp.headers.get("Content-Encoding", "")).strip().lower()

    if encoding in ("gzip", "x-gzip") and raw:
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = decomp.decompress(raw, max_bytes + 1)
        if decomp.unconsumed_tail or len(out) > max_bytes:
            raise RuntimeError(f"Payload Gzip descomprimido excedeu o limite seguro ({max_bytes} bytes).")
        tail = decomp.flush(max_bytes + 1 - len(out))
        out += tail
        if len(out) > max_bytes:
            raise RuntimeError(f"Payload Gzip descomprimido excedeu o limite seguro ({max_bytes} bytes).")
        return out

    return raw


def resolve_safe_db_path(base_dir: Path, cfg_db_path: str | None) -> Path:
    """
    Resolve o caminho do banco SQLite bloqueando Path Traversal e symlinks fora do diretório do projeto,
    exceto quando explicitamente definido pela variável de ambiente confiável SQLITE_DB_PATH (ex.: Docker).
    """
    env_db = os.environ.get("SQLITE_DB_PATH", "").strip()
    if env_db:
        env_candidate = Path(env_db)
        if env_candidate.is_symlink():
            raise RuntimeError(f"Caminho SQLITE_DB_PATH não pode ser symlink: {env_candidate}")
        return env_candidate.resolve()

    base_resolved = base_dir.resolve()
    raw = str(cfg_db_path or "./ranking_cache.db").strip() or "./ranking_cache.db"
    candidate = Path(raw)
    raw_target = candidate if candidate.is_absolute() else (base_resolved / candidate)
    if raw_target.is_symlink():
        raise RuntimeError(f"Caminho do banco SQLite não pode ser symlink: {raw_target}")
    resolved = raw_target.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"Caminho de banco SQLite fora do diretório seguro bloqueado: {raw}") from exc
    return resolved


def atomic_write_bytes(target_path: Path, content: bytes) -> None:
    """Escreve arquivo binário atomicamente via arquivo temporário imprevisível com O_NOFOLLOW, bloqueando symlinks."""
    if target_path.is_symlink():
        raise RuntimeError(f"Escrita bloqueada em symlink não confiável: {target_path}")
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp_path, flags, 0o644)
        with os.fdopen(fd, "wb") as tmp_fp:
            tmp_fp.write(content)
        if target_path.is_symlink():
            raise RuntimeError(f"Escrita bloqueada em symlink não confiável: {target_path}")
        os.replace(tmp_path, target_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(target_path: Path, content: str) -> None:
    """Escreve arquivo de texto UTF-8 atomicamente via arquivo temporário imprevisível, bloqueando symlinks."""
    atomic_write_bytes(target_path, content.encode("utf-8"))
