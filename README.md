# tesira2mqtt

A standalone Docker microservice that bridges a **Biamp Tesira Server I/O** audio system to **Home Assistant** via MQTT. It communicates with the Tesira using the native Tesira Text Protocol (TTP) over Telnet and exposes each zone as a set of HA entities through MQTT Auto-Discovery — no custom component or HACS installation required.

---

## Features

- **Volume control** — per-zone level slider with configurable dB range
- **Mute** — per-zone mute toggle
- **Source routing** — per-zone input selector driven by the Tesira matrix mixer
- **Live state sync** — TTP push subscriptions keep HA up to date when levels change in Tesira Designer or from any other control system
- **HA Auto-Discovery** — entities appear automatically under a single device; no manual YAML configuration in HA
- **Retained MQTT state** — routing state survives HA or bridge restarts without needing to re-query the hardware
- **Auto-reconnect** — recovers from Tesira or MQTT connection drops

---

## Architecture

```
Tesira Server I/O
      │  TCP port 23
      │  Tesira Text Protocol (TTP)
      ▼
 tesira2mqtt (Docker)
      │  MQTT
      ▼
 Mosquitto broker
      │  MQTT Auto-Discovery
      ▼
 Home Assistant
```

The bridge maintains a persistent Telnet connection to the Tesira, subscribes to level and mute changes via TTP push notifications, and mirrors all state to MQTT topics that HA reads in real time.

---

## Tesira Designer Setup

This is the most important prerequisite. The bridge uses instance tags — **not** the display names visible in the Tesira Designer canvas — to address DSP blocks over TTP.

### Finding instance tags

In Tesira Designer, right-click any block → **Properties** → the **Instance Tag** field is what you need. It is distinct from the display name shown on the block label. Instance tags are case-sensitive.

> **The configuration must be deployed** from Tesira Designer to the device before any TTP commands will work. If the configuration has not been deployed, every TTP command returns `-ERR address not found`, even for valid instance tags.

### Required blocks

#### Level blocks (one per zone)

Each output zone needs a **Level** block in your Tesira design. The bridge uses this block for both volume and mute control — Level blocks expose both `level` and `mute` attributes on the same instance tag, so no separate Mute block is required.

- Add one Level block per zone in your signal chain, between the matrix output and any amplifier inputs.
- Set a unique, descriptive **Instance Tag** for each (e.g. `level_z1`, `level_z2`, …).
- The bridge uses channel 1 (Left) as the representative channel for stereo zones and controls both channels together.

#### Matrix Mixer block (one total)

Source routing requires a **Matrix Mixer** block. The bridge enables and disables crosspoints using the `crosspointLevelState` attribute (a boolean enable/disable flag), which is the correct routing attribute for the `MatrixMixerInterface` type — not `crosspointLevel` (gain) or `crosspointMute`.

- Add a single Matrix Mixer block. All sources connect to its inputs; all zone feeds come from its outputs.
- Set a unique **Instance Tag** (e.g. `matrix`).
- Wire your source inputs to specific input channel numbers and your zone outputs to specific output channel numbers. These channel numbers are what you configure in `config.yaml`.

**Example wiring for a 3-source, 10-zone system:**

| Input ch | Source        |
|----------|---------------|
| 1–2      | Sonos Port (L/R) |
| 3–4      | Turntable (L/R)    |
| 15–16    | Aux In (L/R) |

| Output ch | Zone    |
|-----------|---------|
| 1–2       | Zone 1  |
| 3–4       | Zone 2  |
| …         | …       |
| 19–20     | Zone 10 |

> **Channel convention:** odd = Left, even = Right throughout this design.

---

## Installation

### Prerequisites

- Docker + Docker Compose (or Portainer)
- A running Mosquitto MQTT broker reachable from the container
- Tesira Server I/O on the same LAN, with Tesira Designer configuration deployed

### 1. Clone the repository

```bash
git clone git@github.com:jminnick790/tesira2mqtt.git
cd tesira2mqtt
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your connection details:

```env
TESIRA_HOST=192.168.1.50

MQTT_HOST=192.168.1.20
MQTT_USERNAME=tesira
MQTT_PASSWORD=yourpassword

LOG_LEVEL=INFO
```

The `.env` file is gitignored and never baked into the image. Sensitive credentials stay out of `config.yaml` and version control.

### 3. Configure zones and routing

Copy the example config and edit it:

```bash
cp config/config.yaml.example config/config.yaml
```

Edit `config/config.yaml` — see [Configuration Reference](#configuration-reference) below.

### 4. Build and run

```bash
docker compose up -d --build
```

On first startup the bridge publishes MQTT Auto-Discovery payloads and all entities appear in HA under **Settings → Devices & Services → MQTT → Biamp Tesira** (or whatever `device_name` you set).

---

## Configuration Reference

`config/config.yaml` has four top-level sections.

### `tesira`

```yaml
tesira:
  host: "192.168.1.50"          # Tesira IP address
  port: 23                      # Telnet port — do not change
  username: "default"           # Tesira login username
  password: "default"           # Tesira login password
  reconnect_interval_s: 5       # seconds between reconnect attempts
  subscription_min_rate_ms: 100 # minimum push subscription rate
```

Connection credentials can instead be set via env vars (`TESIRA_HOST`, `TESIRA_USERNAME`, `TESIRA_PASSWORD`) — env vars take precedence over the YAML values.

### `mqtt`

```yaml
mqtt:
  host: "192.168.1.20"
  port: 1883
  username: ""
  password: ""
  discovery_prefix: "homeassistant"   # must match HA's discovery prefix
  device_name: "Biamp Tesira"         # name shown in HA device registry
  device_id: "tesira_main"            # unique ID for the HA device
```

### `sources`

Logical names for your audio sources. These map to matrix mixer input channels in the routing section.

```yaml
sources:
  - id: sonos_port
    name: "Sonos Port"
  - id: turntable
    name: "Turntable"
  - id: aux_in
    name: "Aux In"
```

### `zones`

One entry per output zone. `level_instance` must exactly match the **Instance Tag** of the Level block in Tesira Designer.

```yaml
zones:
  - id: zone_01
    name: "Kitchen"           # shown in HA entity names
    level_instance: "level_z1"
    level_channel: 0          # 0 = use ch1 (Left) as representative for stereo
    min_db: -100.0
    max_db: 0.0
```

| Field | Description |
|-------|-------------|
| `id` | Internal identifier — used to link zones to routing entries |
| `name` | Human-readable name — drives HA entity names (e.g. "Kitchen Volume", "Kitchen Mute", "Kitchen Source") |
| `level_instance` | Instance tag of the Tesira Level block |
| `level_channel` | Channel to control. Use `0` for ganged stereo (bridge maps this to ch1 internally) |
| `min_db` / `max_db` | Fallback dB range for the HA volume slider. On startup the bridge queries the hardware and overrides these values automatically — set them conservatively (e.g. `-100.0` / `0.0`) in case the query fails |
| `mute_instance` | Optional — only needed if mute is on a separate block from the Level block |

### `routing`

One entry per zone that has source selection. `matrix_instance` must exactly match the **Instance Tag** of the Matrix Mixer block. The `name` field is optional — the bridge derives the entity name automatically from the zone name (e.g. zone named "Kitchen" → entity named "Kitchen Source").

```yaml
routing:
  - id: zone_01_source
    zone_id: zone_01              # must match a zones[].id
    matrix_instance: "matrix"    # Matrix Mixer instance tag
    output_channels: [1, 2]      # matrix output channels for this zone (L, R)
    sources:
      - source_id: sonos_port    # must match a sources[].id
        input_channels: [1, 2]   # matrix input channels for this source (L, R)
      - source_id: turntable
        input_channels: [3, 4]
      - source_id: aux_in
        input_channels: [15, 16]
```

The source selector in HA will always include an **Off** option at the top, which disables all crosspoints for that zone.

---

## Environment Variables

All connection settings can be overridden at runtime via environment variables (useful for Docker secrets or CI):

| Variable | Description | Default |
|----------|-------------|---------|
| `TESIRA_HOST` | Tesira IP address | from config.yaml |
| `TESIRA_PORT` | Tesira Telnet port | `23` |
| `TESIRA_USERNAME` | Tesira username | `default` |
| `TESIRA_PASSWORD` | Tesira password | `default` |
| `MQTT_HOST` | MQTT broker address | from config.yaml |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `MQTT_USERNAME` | MQTT username | `""` |
| `MQTT_PASSWORD` | MQTT password | `""` |
| `CONFIG_PATH` | Path to config.yaml inside container | `config/config.yaml` |
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) | `INFO` |

Set `LOG_LEVEL=DEBUG` to see raw TTP traffic — useful for verifying instance tags and diagnosing connection issues.

---

## MQTT Topics

The bridge uses the following topic structure:

| Topic | Direction | Description |
|-------|-----------|-------------|
| `tesira/bridge/status` | bridge → HA | `online` / `offline` (LWT) |
| `tesira/zone/{id}/level/state` | bridge → HA | Current level in dB |
| `tesira/zone/{id}/level/set` | HA → bridge | Set level in dB |
| `tesira/zone/{id}/mute/state` | bridge → HA | `ON` or `OFF` |
| `tesira/zone/{id}/mute/set` | HA → bridge | `ON` or `OFF` |
| `tesira/routing/{id}/state` | bridge → HA | Active source name or `Off` (retained) |
| `tesira/routing/{id}/set` | HA → bridge | Source name or `Off` |

---

## Deployment with Portainer

When moving from a dev machine to a server:

1. Push your image or rebuild on the server (the `config/` volume and `.env` file are never baked in).
2. Copy `config/config.yaml` and `.env` to the same paths on the server.
3. Deploy the stack in Portainer — the bridge will reconnect to the same Tesira and MQTT broker.
4. HA will recognize the same device (matched by `device_id`) and re-link all existing entities automatically — no setup required in HA.

---

## Troubleshooting

**Entities don't appear in HA**
- Confirm the MQTT integration is enabled in HA and pointed at the same broker.
- Check that `discovery_prefix` in config matches HA's setting (default: `homeassistant`).

**`-ERR address not found` in logs**
- Verify the instance tag in `config.yaml` exactly matches the **Instance Tag** field in Tesira Designer (not the display name on the block).
- Confirm the Tesira configuration has been **deployed** from Designer to the device. All TTP commands fail with this error if the config is not deployed.

**Volume/mute commands have no effect**
- Set `LOG_LEVEL=DEBUG` and confirm TTP commands are being sent and returning `+OK`.
- Check that `level_channel` is set to `0` (not a specific channel number) for stereo ganged zones.

**Source select shows "unknown" after restart**
- Routing state is published as retained MQTT messages and should persist. If the topic has been cleared, select any source to force a re-publish.

---

## Development & Releases

### Branching strategy

| Branch / ref | Purpose |
|---|---|
| `main` | Stable, always deployable. Merging here triggers a `:latest` image build. |
| `dev` | Integration branch for work-in-progress. No automatic build. |
| Feature branches | Short-lived branches off `dev` (e.g. `feat/zone-groups`). Open a PR to `dev` when ready. |
| `v*` tags | Trigger a versioned release build (e.g. `v1.2.0` → image tagged `:1.2.0`, `:1.2`, and `:latest`). |

### Releasing a new version

```bash
git tag v1.2.0
git push origin v1.2.0
```

GitHub Actions will build and push the image automatically. The published image is available at:

```
ghcr.io/jminnick790/tesira2mqtt:latest
ghcr.io/jminnick790/tesira2mqtt:1.2.0
```

### Pulling the pre-built image (Portainer / Docker)

Instead of building locally, you can pull directly from the registry. Update `docker-compose.yml` to reference the published image:

```yaml
services:
  tesira2mqtt:
    image: ghcr.io/jminnick790/tesira2mqtt:latest
    container_name: tesira2mqtt
    restart: unless-stopped
    volumes:
      - ./config:/config:ro
    environment:
      - CONFIG_PATH=/config/config.yaml
      - LOG_LEVEL
      - TESIRA_HOST
      - MQTT_HOST
      - MQTT_USERNAME
      - MQTT_PASSWORD
    network_mode: host
```

Then in Portainer, deploy this stack and it will pull the image from ghcr.io automatically. No local build required. Watchtower will keep it updated if you include the `watchtower` label.

---

## License

MIT
