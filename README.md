# BirdNET-Go for TRMNL

BirdNET-Go for TRMNL turns a private BirdNET-Go station into a quiet daily bird dashboard on a TRMNL e-ink display. A small local adapter reads BirdNET-Go's HTTP API and pushes a compact snapshot to a TRMNL Private Plugin webhook. BirdNET-Go remains private and accepts no inbound Internet traffic.

The full layout features the latest species portrait, today's detection and species totals, top species, recent calls, and hourly activity. Half-screen and quadrant layouts are included for mashups. The designs support both TRMNL X and TRMNL OG.

## How it works

```text
BirdNET-Go localhost API -> birdnet-trmnl timer -> TRMNL webhook -> e-ink display
```

The adapter sends a complete snapshot every ten minutes. Each successful API read produces a fresh `checked_at` timestamp, so a retained e-ink screen clearly shows when its data became stale. A failed BirdNET or network request leaves the last good screen intact.

Bird portraits come from free-license Wikimedia images. The adapter caches portrait metadata for seven days and includes the photographer and license on the full layout. If a freely licensed image or attribution is unavailable, the recipe shows a built-in bird silhouette.

Counts are BirdNET-Go acoustic detections, not estimates of individual birds.

## Requirements

- BirdNET-Go with API v2, including `/api/v2/analytics/species/daily`
- Python 3.11 or newer on the BirdNET-Go host
- A TRMNL account with Private Plugin support
- Outbound HTTPS access to `trmnl.com`, `en.wikipedia.org`, and `commons.wikimedia.org`

## 1. Create the TRMNL plugin

Build the importable recipe archive:

```sh
./scripts/build-recipe.sh
```

In TRMNL, create or import a Private Plugin with the **Webhook** strategy. Import `dist/birdnet-go-trmnl-recipe.zip`, save it, and copy the generated Webhook URL. Do not commit or share that URL; possession of its UUID permits updates to the plugin.

The webhook API allows 12 pushes per hour and 2,000 bytes per request on standard accounts. The supplied ten-minute timer uses six pushes per hour, and the adapter removes optional rows or image metadata if needed to keep the complete HTTP body at or below 2,000 bytes.

## 2. Install the adapter

Copy or clone this project onto the same Linux host as BirdNET-Go, then run:

```sh
sudo ./scripts/install.sh
sudoedit /etc/birdnet-trmnl.env
```

Set `BIRDNET_TRMNL_WEBHOOK_URL` to the URL copied from TRMNL. Review the station name and timezone, then test one collection without sending anything:

```sh
sudo sh -c 'set -a; . /etc/birdnet-trmnl.env; exec python3 /usr/local/libexec/birdnet-trmnl --dry-run'
```

Send and inspect the first update:

```sh
sudo systemctl start birdnet-trmnl.service
sudo systemctl status birdnet-trmnl.service
sudo journalctl -u birdnet-trmnl.service -n 30 --no-pager
```

Enable automatic updates once the test succeeds:

```sh
sudo systemctl enable --now birdnet-trmnl.timer
systemctl list-timers birdnet-trmnl.timer
```

The service uses a transient system user, a root-only environment file, and systemd filesystem protections. Its only writable locations are the state and image-cache directories created by systemd.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BIRDNET_TRMNL_BIRDNET_URL` | `http://127.0.0.1:8080` | BirdNET-Go base URL |
| `BIRDNET_TRMNL_WEBHOOK_URL` | required to send | Secret TRMNL Private Plugin URL |
| `BIRDNET_TRMNL_STATION_NAME` | `BirdNET-Go` | Dashboard title |
| `BIRDNET_TRMNL_TIMEZONE` | `UTC` | IANA timezone used for the daily boundary |
| `BIRDNET_TRMNL_MIN_CONFIDENCE` | `0` | Optional extra confidence floor, from `0` to `1` |
| `BIRDNET_TRMNL_WIKIMEDIA_IMAGES` | `true` | Enable public portrait lookup |
| `BIRDNET_TRMNL_MAX_PAYLOAD_BYTES` | `2000` | Complete webhook body limit |
| `BIRDNET_TRMNL_TIMEOUT_SECONDS` | `10` | Per-request HTTP timeout |

BirdNET-Go's own confidence threshold remains authoritative by default. Reviewed false positives are omitted from the recent-detections section; the analytics endpoint supplies the daily aggregates.

## Development

Run the dependency-free test suite and collect a live dry-run payload:

```sh
python3 -m unittest discover -s tests -v
BIRDNET_TRMNL_BIRDNET_URL=http://birdnet-host:8080 \
  BIRDNET_TRMNL_TIMEZONE=America/New_York \
  python3 birdnet_trmnl.py --dry-run
```

The recipe is structured for the official [`trmnlp`](https://github.com/usetrmnl/trmnlp) preview tool. From the repository root:

```sh
docker run --rm -p 4567:4567 -v "$PWD/recipe:/plugin" trmnl/trmnlp serve --bind 0.0.0.0
```

Open `http://localhost:4567/full` and select TRMNL X to inspect the principal layout. Sample variables live in `.trmnlp.yml`; the exact example webhook body is in `recipe/data/sample_payload.json`.

## Uninstall

```sh
sudo ./scripts/uninstall.sh
```

The uninstaller preserves `/etc/birdnet-trmnl.env` and cached image metadata. Remove those separately if you no longer need them.

## License

MIT
