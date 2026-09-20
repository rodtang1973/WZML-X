"""Route AllDebrid traffic through a proxy, and nothing else.

The work happens in patcher.py. This file owns the lifecycle and the /adproxy
panel.
"""

from html import escape
from os import getenv
from time import monotonic

from bot import LOGGER
from bot.core.config_manager import Config
from bot.core.plugin_manager import PluginBase, get_plugin_manager
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import edit_message, send_message

from .patcher import (
    BUILTIN_HOSTS,
    STATE,
    install,
    installed,
    parse_proxy,
    remove,
    socks_available,
)

PLUGIN = "alldebrid_proxy"
_TEST_URL = "https://api.alldebrid.com/v4/user"
_IP_URL = "https://api.ipify.org"


def _config():
    try:
        return get_plugin_manager().effective_config(PLUGIN) or {}
    except Exception as err:
        LOGGER.error(f"{PLUGIN}: could not read settings: {err}")
        return {}


def _fallback_url():
    """Let a bot-level var or env var stand in when no setting is given."""
    return str(getattr(Config, "ALLDEBRID_PROXY", "") or getenv("ALLDEBRID_PROXY", "") or "")


def _apply(config=None):
    STATE.configure(config if config is not None else _config(), _fallback_url())
    for problem in STATE.errors:
        LOGGER.error(f"{PLUGIN}: {problem}")
    return STATE


def _mask(raw):
    raw = (raw or "").strip()
    if not raw:
        return "not set"
    full, base, scheme, user, password, err = parse_proxy(raw)
    if err:
        return f"invalid - {err}"
    if user or password:
        host = base.split("://", 1)[-1]
        return f"{scheme}://{user}:****@{host}"
    return base


def _panel():
    cfg = _config()
    lines = ["<b>AllDebrid Proxy</b>", ""]

    lines.append(f"<b>Proxy:</b> <code>{escape(_mask(cfg.get('proxy_url') or _fallback_url()))}</code>")
    if dl := (cfg.get("download_proxy_url") or "").strip():
        lines.append(f"<b>Download proxy:</b> <code>{escape(_mask(dl))}</code>")

    api = "on" if STATE.proxy_api else "off"
    dls = "on" if STATE.proxy_downloads else "off"
    lines.append(f"<b>API calls:</b> {api}   <b>Downloads:</b> {dls}")
    lines.append(
        f"<b>Proxied so far:</b> {STATE.stats['api']} api, {STATE.stats['download']} downloads"
    )
    lines.append("")

    lines.append(f"<b>Hosts:</b> <code>{escape(', '.join(sorted(BUILTIN_HOSTS)))}</code>")
    if extra := sorted(set(STATE.suffixes) - set(BUILTIN_HOSTS)):
        lines.append(f"<b>Extra:</b> <code>{escape(', '.join(extra))}</code>")
    if STATE.learned:
        learned = ", ".join(list(STATE.learned)[-6:])
        lines.append(
            f"<b>Learned ({len(STATE.learned)}):</b> <code>{escape(learned)}</code>"
        )

    if STATE.errors:
        lines.append("")
        for problem in STATE.errors:
            lines.append(f"⚠️ <i>{escape(problem)}</i>")
    if STATE.notes:
        lines.append("")
        for note in STATE.notes:
            lines.append(f"<i>{escape(note)}</i>")
    if not installed():
        lines.append("")
        lines.append("<i>Patches are not attached - the plugin is disabled.</i>")
    return "\n".join(lines)


def _buttons(user_id):
    buttons = ButtonMaker()
    buttons.data_button("Test", f"adproxy {user_id} test")
    buttons.data_button("Refresh", f"adproxy {user_id} refresh")
    buttons.data_button("Close", f"adproxy {user_id} close", position="footer")
    return buttons.build_menu(2)


async def _probe(url, proxies, **kwargs):
    from niquests import AsyncSession

    async with AsyncSession() as client:
        return await client.get(url, proxies=proxies, timeout=20, **kwargs)


async def _run_test():
    cfg = _config()
    raw = str(cfg.get("proxy_url") or "").strip() or _fallback_url()
    if not raw:
        return "<b>AllDebrid Proxy - test</b>\n\n<i>No proxy_url is set.</i>"

    full, base, scheme, user, password, err = parse_proxy(raw)
    if err:
        return f"<b>AllDebrid Proxy - test</b>\n\n⚠️ <i>proxy_url {escape(err)}</i>"
    if scheme.startswith("socks") and not socks_available():
        return (
            "<b>AllDebrid Proxy - test</b>\n\n⚠️ <i>SOCKS proxy but python-socks is "
            "missing.</i>\n<code>pip install 'niquests[socks]'</code>"
        )

    proxies = {"http": full, "https": full}
    lines = ["<b>AllDebrid Proxy - test</b>", ""]

    started = monotonic()
    try:
        response = await _probe(
            _TEST_URL,
            proxies,
            params={"agent": "wzmlx", "apikey": (Config.ALLDEBRID_API_KEY or "").strip()},
        )
        elapsed = int((monotonic() - started) * 1000)
        payload = response.json()
        if payload.get("status") == "success":
            username = (payload.get("data", {}).get("user", {}) or {}).get("username", "?")
            premium = (payload.get("data", {}).get("user", {}) or {}).get("isPremium")
            lines.append(f"✅ <b>API reachable</b> via proxy in {elapsed} ms")
            lines.append(
                f"<b>Account:</b> <code>{escape(str(username))}</code>"
                f"{' (premium)' if premium else ''}"
            )
        else:
            error = (payload.get("error") or {}).get("message", "unknown")
            lines.append(f"⚠️ <b>Proxy reachable</b> in {elapsed} ms, but AllDebrid said:")
            lines.append(f"<i>{escape(str(error))}</i>")
    except Exception as err:
        lines.append("❌ <b>Could not reach AllDebrid through the proxy</b>")
        lines.append(f"<i>{escape(str(err))}</i>")

    try:
        ip_response = await _probe(_IP_URL, proxies)
        lines.append("")
        lines.append(f"<b>Egress IP:</b> <code>{escape(ip_response.text.strip())}</code>")
    except Exception:
        lines.append("")
        lines.append("<i>Egress IP lookup failed (harmless).</i>")

    if STATE.proxy_downloads:
        lines.append(f"<b>aria2 all-proxy:</b> <code>{escape(STATE.dl_base)}</code>")
    else:
        lines.append("<b>aria2 all-proxy:</b> <i>downloads not proxied</i>")
    return "\n".join(lines)


@new_task
async def adproxy_command(_, message):
    _apply()
    await send_message(message, _panel(), _buttons(message.from_user.id))


@new_task
async def adproxy_callback(_, query):
    data = query.data.split()
    if len(data) < 3 or query.from_user.id != int(data[1]):
        return await query.answer("Not yours!", show_alert=True)
    action = data[2]

    if action == "close":
        await query.answer()
        return await edit_message(query.message, "<i>Closed.</i>")

    if action == "refresh":
        await query.answer("Refreshed")
        _apply()
        return await edit_message(query.message, _panel(), _buttons(query.from_user.id))

    if action == "test":
        await query.answer("Testing...")
        await edit_message(query.message, "<i>Testing the proxy...</i>")
        result = await _run_test()
        return await edit_message(query.message, result, _buttons(query.from_user.id))


class AllDebridProxyPlugin(PluginBase):
    async def on_load(self):
        _apply()
        install()
        LOGGER.info(
            f"{PLUGIN}: attached (api={STATE.proxy_api}, downloads={STATE.proxy_downloads})"
        )
        return True

    async def on_unload(self):
        remove()
        LOGGER.info(f"{PLUGIN}: detached")
        return True

    async def on_enable(self):
        _apply()
        install()
        return True

    async def on_disable(self):
        remove()
        LOGGER.info(f"{PLUGIN}: detached (disabled)")
        return True

    async def on_configure(self, config):
        _apply(config)
        if installed():
            LOGGER.info(
                f"{PLUGIN}: reconfigured (api={STATE.proxy_api}, "
                f"downloads={STATE.proxy_downloads})"
            )
        return True
