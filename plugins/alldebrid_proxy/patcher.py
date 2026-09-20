"""Host-scoped proxy patches for AllDebrid traffic.

Three runtime patches, all reversible. Every one of them decides per
destination host, so traffic that is not AllDebrid's is never touched:

1. ``niquests.AsyncSession.request`` -- the AllDebrid API calls. This class is
   shared by five unrelated modules (crash reporter, telegraph, bot_utils), so
   the patch proxies by host rather than by session.
2. ``aioaria2.client._Aria2BaseClient.addUri`` -- the actual file bytes.
   Patched on the class, not the instance, so it survives the reconnect in
   ``TorrentManager.connect`` that swaps in a brand new client object.
3. ``alldebrid_resolver._call_api`` -- learns AllDebrid's delivery hosts from
   the API responses, because the CDN does not live under alldebrid.com.
"""

from collections import OrderedDict
from importlib.util import find_spec
from urllib.parse import urlparse, urlunparse, unquote

from bot import LOGGER

BUILTIN_HOSTS = ("alldebrid.com", "debrid.it")

# Schemes niquests can dial. SOCKS needs the python-socks package.
API_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
# aria2 has no SOCKS support at all -- it assumes http:// whatever you give it.
ARIA_SCHEMES = ("http", "https")

_LEARNED_MAX = 256


def socks_available():
    try:
        return find_spec("python_socks") is not None
    except (ImportError, ValueError):
        return False


def host_of(url):
    try:
        return (urlparse(str(url)).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def parse_proxy(raw):
    """Split a proxy URL into its parts.

    Returns ``(full, base, scheme, user, password, error)`` where ``full``
    keeps any credentials inline (what niquests wants) and ``base`` has them
    stripped (what aria2 wants, with the credentials passed separately).
    """
    raw = (raw or "").strip()
    if not raw:
        return "", "", "", "", "", ""
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parts = urlparse(raw)
    except Exception as err:
        return "", "", "", "", "", f"could not be parsed ({err})"
    scheme = (parts.scheme or "").lower()
    if not parts.hostname:
        return "", "", "", "", "", "has no host"
    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    base = urlunparse((scheme, netloc, "", "", "", ""))
    return raw, base, scheme, user, password, ""


class ProxyState:
    """Everything the patches read. Mutated in place by on_configure."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.api_proxy = ""
        self.dl_base = ""
        self.dl_user = ""
        self.dl_pass = ""
        self.proxy_api = False
        self.proxy_downloads = False
        self.learn_hosts = True
        self.verbose = False
        self.suffixes = set(BUILTIN_HOSTS)
        self.learned = OrderedDict()
        self.errors = []
        self.notes = []
        self.stats = {"api": 0, "download": 0}

    def matches(self, host):
        if not host:
            return False
        host = host.lower().rstrip(".")
        if host in self.learned:
            return True
        return any(
            host == suffix or host.endswith(f".{suffix}") for suffix in self.suffixes
        )

    def learn(self, host):
        if not host or host in self.learned or self.matches(host):
            return
        self.learned[host] = True
        while len(self.learned) > _LEARNED_MAX:
            self.learned.popitem(last=False)
        if self.verbose:
            LOGGER.info(f"alldebrid_proxy: learned delivery host {host}")

    def configure(self, config, fallback_url=""):
        """Rebuild state from plugin settings. Never raises."""
        self.errors = []
        self.notes = []
        self.learned = OrderedDict() if not config.get("learn_hosts", True) else self.learned

        self.verbose = bool(config.get("verbose", False))
        self.learn_hosts = bool(config.get("learn_hosts", True))
        self.suffixes = set(BUILTIN_HOSTS)
        for extra in config.get("extra_hosts") or []:
            if cleaned := str(extra).strip().lower().lstrip("*.").rstrip("."):
                self.suffixes.add(cleaned)

        raw = str(config.get("proxy_url") or "").strip() or fallback_url
        full, base, scheme, user, password, err = parse_proxy(raw)

        self.api_proxy = ""
        self.proxy_api = False
        if not raw:
            self.notes.append("No proxy URL set - nothing is being proxied.")
        elif err:
            self.errors.append(f"proxy_url {err}")
        elif scheme not in API_SCHEMES:
            self.errors.append(
                f"proxy_url scheme '{scheme}' is not supported "
                f"(use one of: {', '.join(API_SCHEMES)})"
            )
        elif scheme.startswith("socks") and not socks_available():
            self.errors.append(
                "proxy_url is SOCKS but the socks extra is missing - "
                "run: pip install 'niquests[socks]'"
            )
        elif config.get("proxy_api", True):
            self.api_proxy = full
            self.proxy_api = True

        # Downloads: aria2 only, so HTTP only.
        self.dl_base = self.dl_user = self.dl_pass = ""
        self.proxy_downloads = False
        if not config.get("proxy_downloads", True):
            return
        dl_raw = str(config.get("download_proxy_url") or "").strip()
        explicit = bool(dl_raw)
        if not dl_raw:
            dl_raw = raw
        if not dl_raw:
            return
        d_full, d_base, d_scheme, d_user, d_pass, d_err = parse_proxy(dl_raw)
        if d_err:
            self.errors.append(f"download_proxy_url {d_err}")
        elif d_scheme not in ARIA_SCHEMES:
            which = "download_proxy_url" if explicit else "proxy_url"
            self.errors.append(
                f"downloads NOT proxied: aria2 has no SOCKS support, but {which} "
                f"is '{d_scheme}'. Set download_proxy_url to an http:// proxy."
            )
        else:
            self.dl_base, self.dl_user, self.dl_pass = d_base, d_user, d_pass
            self.proxy_downloads = True


STATE = ProxyState()

_orig_request = None
_orig_add_uri = None
_orig_call_api = None
_resolver_module = None


def _harvest(payload, depth=0):
    """Pull delivery hosts out of an AllDebrid API response."""
    if depth > 6:
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if (
                key in ("link", "url")
                and isinstance(value, str)
                and value.startswith("http")
            ):
                STATE.learn(host_of(value))
            else:
                _harvest(value, depth + 1)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            _harvest(item, depth + 1)


def install():
    """Attach all three patches. Idempotent."""
    global _orig_request, _orig_add_uri, _orig_call_api, _resolver_module

    if _orig_request is None:
        from niquests import AsyncSession

        _orig_request = AsyncSession.request

        async def request(self, method, url, *args, **kwargs):
            if (
                STATE.proxy_api
                and STATE.api_proxy
                and not kwargs.get("proxies")
                and STATE.matches(host_of(url))
            ):
                kwargs["proxies"] = {
                    "http": STATE.api_proxy,
                    "https": STATE.api_proxy,
                }
                STATE.stats["api"] += 1
                if STATE.verbose:
                    LOGGER.info(f"alldebrid_proxy: {method} {host_of(url)} via proxy")
            return await _orig_request(self, method, url, *args, **kwargs)

        AsyncSession.request = request

    if _orig_add_uri is None:
        from aioaria2.client import _Aria2BaseClient

        _orig_add_uri = _Aria2BaseClient.addUri

        async def add_uri(self, uris, options=None, position=None):
            if (
                STATE.proxy_downloads
                and STATE.dl_base
                and uris
                and all(STATE.matches(host_of(u)) for u in uris)
            ):
                # Copy: DirectListener reuses one options dict for a whole batch.
                options = dict(options or {})
                options["all-proxy"] = STATE.dl_base
                if STATE.dl_user:
                    options["all-proxy-user"] = STATE.dl_user
                if STATE.dl_pass:
                    options["all-proxy-passwd"] = STATE.dl_pass
                STATE.stats["download"] += 1
                if STATE.verbose:
                    LOGGER.info(
                        f"alldebrid_proxy: aria2 {host_of(uris[0])} via {STATE.dl_base}"
                    )
            return await _orig_add_uri(self, uris, options, position)

        _Aria2BaseClient.addUri = add_uri

    if _orig_call_api is None:
        from bot.helper.mirror_leech_utils.download_utils import alldebrid_resolver

        _resolver_module = alldebrid_resolver
        _orig_call_api = alldebrid_resolver._call_api

        async def call_api(*args, **kwargs):
            data = await _orig_call_api(*args, **kwargs)
            if STATE.learn_hosts:
                try:
                    _harvest(data)
                except Exception as err:
                    LOGGER.error(f"alldebrid_proxy: host learning failed: {err}")
            return data

        alldebrid_resolver._call_api = call_api


def remove():
    """Detach everything and leave the originals exactly as found."""
    global _orig_request, _orig_add_uri, _orig_call_api, _resolver_module

    if _orig_request is not None:
        from niquests import AsyncSession

        AsyncSession.request = _orig_request
        _orig_request = None

    if _orig_add_uri is not None:
        from aioaria2.client import _Aria2BaseClient

        _Aria2BaseClient.addUri = _orig_add_uri
        _orig_add_uri = None

    if _orig_call_api is not None and _resolver_module is not None:
        _resolver_module._call_api = _orig_call_api
        _orig_call_api = None
        _resolver_module = None


def installed():
    return _orig_request is not None
