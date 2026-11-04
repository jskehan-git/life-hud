# Life HUD

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
Copyright 2026 Joseph D. Skehan.

A home TV dashboard in pure Python (stdlib only — no pip, no Node).
Weather, upcoming tasks, system gauges (CPU / memory / temperature),
a US precipitation radar loop, and an MP3 player with album art and
synced lyrics, served as a single web page for a computer hooked to a TV.

## Run

```bash
cp tasks.json.example tasks.json   # then edit tasks.json with your tasks
python3 life-hud.py                # serves http://127.0.0.1:8080
```

`start.sh` keeps it running and restarts it if it exits.

## Configure (environment variables)

| Variable | Default | What it does |
|---|---|---|
| `LIFE_HUD_HOST` | `127.0.0.1` | Bind address. Set to `0.0.0.0` only on a trusted home network if other devices need to reach it. |
| `LIFE_HUD_PORT` | `8080` | Port. |
| `LIFE_HUD_MUSIC` | `./music` | Folder (or symlink/mount) containing MP3s. |
| `LIFE_HUD_LAT` / `LIFE_HUD_LON` | `39.74` / `-104.99` | Weather location (example: Denver, CO). |
| `LIFE_HUD_TZ` | `America/Denver` | Weather timezone. |
| `LIFE_HUD_CITY` | `Denver, CO` | City name shown on the weather card. |

System gadgets read Linux `/proc` and `/sys/class/thermal`, so they
work on Linux only. The NAS Explorer (under the ☰ Menu) uses
`smbclient` if it is installed; it is optional.

## Security — read this before you run it anywhere

This is a **home-network dashboard, not an internet service**.

- **No authentication by design.** Anyone who can reach the port can
  view the dashboard, play music, rescan, and use the NAS Explorer.
  That is acceptable on a TV at home. It is **not** acceptable on the
  open internet.
- **Do not port-forward it.** Do not put it behind a public reverse
  proxy without adding authentication in front of it. For remote
  access, use a VPN (e.g. Tailscale) instead.
- **Default bind is localhost** (`127.0.0.1`) for exactly that reason.
  Changing it to `0.0.0.0` exposes it to your whole LAN.
- **NAS credentials** entered in the NAS Explorer are stored only on
  the machine running the HUD, in `nas.json` (0600) plus a smbclient
  authfile (`.smb-auth`, 0600). They are never sent anywhere except
  to the NAS host you entered, over your own network, in plain HTTP
  to the HUD itself — so enter them only from a device on your own
  network, not over public Wi-Fi. The password is never passed on a
  command line (where `ps` could read it); it goes through the
  authfile instead.
- **Input handling.** NAS paths are rejected if they contain `"`,
  `;`, or newlines (smbclient command injection), and NAS host/share
  names must match a strict hostname/share pattern (no `/`, spaces,
  or metacharacters, so smbclient cannot be pointed at an arbitrary
  server). Music file serving resolves symlinks and refuses files
  outside the music folder, so a `.mp3` symlink to a private file
  cannot be streamed. POST endpoints reject cross-origin requests
  (Origin header must match Host), POST bodies are capped at 64 KB,
  Range requests are validated (out-of-bounds returns 416), and
  rescans are guarded against concurrent runs.
- **Third-party code.** The radar map loads Leaflet from a CDN and
  tiles from OpenStreetMap / RainViewer. Album art comes from iTunes
  and the Cover Art Archive, lyrics from LRCLIB. Those are the only
  external services; weather comes from Open-Meteo with no API key.
- **No secrets in this repo.** Check `.gitignore` before you commit:
  `tasks.json` (your real tasks), `nas.json`, `.smb-auth`,
  `music-index.json`, `music-cache/`, and logs are all excluded.
  Never commit a file with your NAS password, your home address,
  or your real task list.

## What it does

- Weather: Open-Meteo current + 7-day, plus a RainViewer radar loop
  over the US for the past 30 minutes.
- Tasks: from `tasks.json` next to `life-hud.py`.
- System: CPU %, memory %, CPU package temperature, 30-minute
  history graph, load, uptime.
- Music: MP3s under the music folder, with ID3 tags, folder art,
  downloaded art/lyrics (cached, and embedded into local files when
  writable), shuffle queue (reshuffles when it runs out), album and
  artist modes, synced scrolling lyrics with a columns fallback.

## License

Copyright 2026 Joseph D. Skehan

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

The full license text is in [LICENSE](LICENSE).
