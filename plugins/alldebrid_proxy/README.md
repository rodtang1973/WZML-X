# alldebrid_proxy

Sends AllDebrid traffic through a proxy. Everything else — Telegram, Google Drive,
telegraph, crash reports, ordinary direct links — keeps going out normally.

Useful when your AllDebrid account is IP-locked, when AllDebrid is geo-blocked from
your server, or when you want debrid traffic leaving from a different address than
the rest of the bot.

## Setup

1. `/plugins → Installed → alldebrid_proxy → Settings`
2. Set **proxy_url**, e.g. `http://user:pass@1.2.3.4:3128`
3. `/adproxy` → **Test**

That's it. Nothing needs restarting and no bot config file is touched.

## What gets proxied

Two layers, each independently toggleable:

| Layer | Traffic | Setting |
| --- | --- | --- |
| **API** | `api.alldebrid.com` — unlock, magnet upload, status polling, file lists | `proxy_api` |
| **Downloads** | the actual file bytes from AllDebrid's delivery CDN, via aria2 | `proxy_downloads` |

The decision is made **per destination host**, not per connection, which is why the
rest of the bot is unaffected — the bot shares one HTTP client class across five
unrelated modules, and this plugin only attaches a proxy when the host is AllDebrid's.

## SOCKS: read this before using one

**aria2 cannot do SOCKS.** Its manual says it assumes `http://` no matter what scheme
you give it, so a `socks5://` proxy would silently break downloads rather than fail
loudly.

So:

- **HTTP/HTTPS proxy** — both layers work. Nothing else to do.
- **SOCKS proxy** — API calls work, but you must install the extra:
  ```
  pip install 'niquests[socks]'
  ```
  and downloads are **off** unless you also set **download_proxy_url** to a separate
  `http://` proxy. The plugin detects this and says so on `/adproxy` instead of
  letting downloads fail mysteriously.

Use `socks5h://` rather than `socks5://` if you want DNS resolved at the proxy.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `proxy_url` | `""` | The proxy. Empty = nothing is proxied. |
| `proxy_api` | `true` | Proxy `api.alldebrid.com` calls. |
| `proxy_downloads` | `true` | Proxy the file transfer (aria2 `all-proxy`). |
| `download_proxy_url` | `""` | Separate download proxy. Defaults to `proxy_url`. Must be http/https. |
| `extra_hosts` | `[]` | More domains to treat as AllDebrid, comma separated. |
| `learn_hosts` | `true` | Discover AllDebrid's CDN hosts automatically. |
| `verbose` | `false` | Log every proxied request. |

If `proxy_url` is left empty the plugin falls back to `Config.ALLDEBRID_PROXY` (if your
fork adds that variable) and then to the `ALLDEBRID_PROXY` environment variable — so it
works with or without the settings UI.

### Why `learn_hosts` exists

AllDebrid's API is on `alldebrid.com`, but the files it serves come from a separate
delivery network whose hostname varies per node. A fixed domain list would proxy the API
and then let the download go out direct.

So the plugin reads the hosts out of AllDebrid's own API replies and adds them to the
match set as it goes. Nothing to configure. The set holds the 256 most recent hosts,
lives in memory only, and is visible on `/adproxy`.

## `/adproxy`

Owner only. Shows the resolved proxy (password masked), which layers are live, the host
sets, and any configuration problem. **Test** sends a request to AllDebrid through the
proxy and reports reachability, whether your API key authenticates from that egress IP,
the round-trip time, and the egress IP itself.

## Troubleshooting

**Nothing is proxied.** `/adproxy` shows the state and the reason. An empty `proxy_url`
and a rejected scheme both say so explicitly.

**Downloads still go out direct.** Check `/adproxy` for the SOCKS warning. Otherwise set
`verbose: true` and look for `aria2 ... via` lines in `/log`. To confirm at the source,
check `all-proxy` on the live task via aria2's `tellActive`.

**A download host isn't matched.** Turn on `learn_hosts`, or add the domain to
`extra_hosts`. Matching is by suffix, so `debrid.it` covers `s3.debrid.it`.

**Turning it off.** `/plugins → Disable` detaches every patch immediately and restores
the original functions; traffic goes direct again with no restart.

## Notes

- `python_dependencies` is deliberately empty. Declaring a SOCKS package would make the
  plugin refuse to load for everyone on a plain HTTP proxy, since the loader rejects a
  plugin with any missing dependency. SOCKS is checked at runtime instead.
- The aria2 patch is applied to the client **class**, so it survives the reconnect that
  replaces `TorrentManager.aria2` with a new instance.
- Credentials are sent to aria2 as separate `all-proxy-user` / `all-proxy-passwd`
  options rather than inline in the URL, which avoids both URL-encoding problems and
  aria2's ordering-dependent precedence rules.
