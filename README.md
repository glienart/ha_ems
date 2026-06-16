# HA EMS — Home Energy Management System

A Home Assistant add-on that optimizes solar self-consumption, battery cycling, and EV charging using a rule-based engine and real-time EPEX SPOT day-ahead prices. Everything is configured from a built-in ingress dashboard — no YAML required.

![Version](https://img.shields.io/badge/version-0.5.7-blue) ![HA](https://img.shields.io/badge/Home%20Assistant-add--on-41BDF5)

---

## Features

- **24h cost optimizer** — plans the next 24 hours using solar production forecasts (Forecast.Solar API), historical consumption patterns, and EPEX day-ahead prices; the battery follows the schedule, falling back to real-time reactive rules
- **Rule-based optimizer** — decides every N seconds whether to charge/discharge the battery and charge EVs, based on solar production, grid state, tariff, and battery SOC
- **EPEX SPOT prices** — fetches day-ahead prices from the ENTSO-E Transparency Platform; supports 15 European bidding zones
- **Effective tariff formula** — models your real electricity bill as `a × EPEX + b` separately for consumption and injection (covers grid fees, taxes, supplier margin)
- **Interactive dashboard** — live energy flow diagram with animated SVG paths, EPEX bar chart with cheap/expensive zones, decision cards
- **Searchable sensor picker** — comboboxes filtered by unit (W/kW for power, % for SOC, €/kWh for tariff, `switch.*` for switches)
- **Settings persistence** — all configuration is saved to `/data/settings.json` and survives add-on updates
- **Multi-EV support** — configure multiple vehicles, each with its own charger switch, SOC sensor, target SOC, departure time, and charge power; the optimizer decides each independently
- **Virtual HA sensors** — exposes EMS mode, battery decision, per-EV decisions, solar surplus, and reasoning as standard HA entities

---

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store** and click the three-dot menu → **Repositories**
2. Add `https://github.com/glienart/ha_ems`
3. Find **Home Energy Management System** in the store and click **Install**
4. Go to the add-on's **Configuration** tab and set your EPEX token and zone (see [EPEX Setup](#epex-setup))
5. Start the add-on and open the **EMS** panel in the sidebar

---

## Architecture

```
ha_ems/
├── config.yaml          # Add-on manifest (version, arch, ingress, options schema)
├── Dockerfile
├── run.sh
└── app/
    ├── main.py          # FastAPI app + full dashboard HTML/CSS/JS (single file)
    ├── optimizer.py     # Stateful rule-based decision engine (hysteresis, look-ahead, urgency)
    ├── scheduler.py     # 24h greedy cost optimizer (solar forecast + EPEX prices)
    ├── forecast.py      # Forecast.Solar API client + consumption history buffer
    ├── energy_html.py   # Energy tab HTML (flow diagram, EPEX chart, 24h plan)
    ├── settings.py      # Settings dataclass, load/save from /data/
    ├── epex.py          # ENTSO-E API client + XML parser
    ├── ha_client.py     # HA REST API client (get_state, set_entity_state, turn_on/off)
    └── requirements.txt
```

The add-on is a FastAPI app served via HA ingress. The entire frontend is a single-page HTML string embedded in `main.py` — no build step, no separate static files.

**Data flow:**
```
HA sensors → ha_client → run_optimizer() → EmsOptimizer.decide() → HA switches + virtual sensors
                                  ↓
                           _last_state dict → /api/state → dashboard JS
```

---

## Configuration

Only two fields live in the HA **Configuration** tab (because they're needed at startup before the dashboard is reachable):

| Field | Description |
|-------|-------------|
| `epex_token` | ENTSO-E API security token |
| `epex_zone` | Bidding zone short code (e.g. `BE`, `FR`, `DE-LU`) |

All other settings are configured in the dashboard **Settings** tab and persisted to `/data/settings.json`.

### EPEX Setup

1. Register at [transparency.entsoe.eu](https://transparency.entsoe.eu) (free)
2. In your account, go to **My Account Settings → Security Token → Generate a new token**
3. Paste the token into the add-on Configuration tab
4. Set your bidding zone:

| Code | Zone | Code | Zone |
|------|------|------|------|
| `BE` | Belgium | `NL` | Netherlands |
| `FR` | France | `AT` | Austria |
| `DE-LU` | Germany/Luxembourg | `CH` | Switzerland |
| `ES` | Spain | `DK1` | Denmark West |
| `PT` | Portugal | `DK2` | Denmark East |
| `IT-N` | Italy North | `SE3` | Sweden 3 |
| `NO2` | Norway 2 | `FI` | Finland |
| `PL` | Poland | `CZ` | Czech Republic |

---

## Dashboard

The EMS panel has three tabs.

### Dashboard tab

Live cards updated every 5 seconds:

| Card | Description |
|------|-------------|
| Solar surplus | Estimated excess solar production (W) |
| Battery SOC | Current state of charge (%) |
| EV SOC (per vehicle) | One card per configured EV, showing SOC and connection state |
| Buy price | Effective consumption price (€/kWh) after `ax+b` formula; sub-label shows sell price |

**Energy flow diagram** — animated power flow diagram (visible in the Energy tab). Only active flows are shown; inactive paths are hidden. Dot direction indicates flow direction:

- 🟠 Solar → Home (orange): shows when solar > 50 W
- 🟠 Solar → Grid / export (orange): shows when grid < −50 W (exporting)
- 🔵 Grid → Home (blue): shows when grid > 50 W (importing)
- 🟢 Battery → Home (teal): shows when battery is discharging
- 🟢 Battery → Grid (teal): shows when battery is discharging and grid is exporting

**Decisions:**

| Card | Values |
|------|--------|
| Battery decision | charge / discharge / standby / idle |
| EV decision (per vehicle) | charge / pause — one badge per configured vehicle |
| Reason | Plain-text explanation of the current decision |

**Mode selector** — switches between AUTO / ECO / CHEAP / MANUAL / OFF.

### Energy tab

Live stat cards at the top (Solar, Grid, Home, Battery) update every 10 seconds alongside the Dashboard.

The animated **energy flow diagram** and **power history chart** sit side by side below the cards:

- The flow diagram shows only active paths (inactive ones are hidden); PV export to grid is orange, grid import to home is blue, battery flows are teal.
- The power chart plots Solar, Grid, Battery, and Consumption in kW since midnight. Curves are smoothed; all series share common 5-minute buckets so the hover tooltip always shows all four values.

**EPEX SPOT prices:**

- **Day-ahead price chart** — bar chart of today's and tomorrow's hourly prices, colour-coded green/yellow/red relative to today's range; current slot highlighted; refreshes every 15 minutes
- **Price pills** — current, next slot, today min and max
- **Hour schedule table** — scrollable list of all slots with a progress bar; auto-scrolls to the current hour

### Settings tab

Settings are organized in groups with a pencil icon (✎) to open each group for editing. Changes are saved to `/data/settings.json` immediately and survive updates.

#### Power Sensors

All sensors filtered to units W or kW.

| Field | Description |
|-------|-------------|
| Solar production | Power sensor for PV output (W) |
| Grid power | Power sensor for grid exchange (W); positive = import, negative = export |
| House consumption | Optional; if absent, estimated as solar − grid |
| Battery power | Power sensor for battery exchange (W); positive = charging, negative = discharging |

#### Battery

| Field | Default | Description |
|-------|---------|-------------|
| SOC sensor | — | Battery state of charge (%) |
| Charge switch | — | Switch to activate charging |
| Discharge switch | — | Switch to activate discharging |
| Standby switch | — | Switch for standby/hold mode |
| Max charge power | 3000 W | Power cap when charging |
| Max discharge power | 3000 W | Power cap when discharging |
| Min SOC | 10% | Never discharge below this |
| Max SOC | 95% | Never charge above this |

#### EV Fleet

You can configure any number of vehicles. Each vehicle has its own card in the Settings tab with the following fields:

| Field | Default | Description |
|-------|---------|-------------|
| Name | EV | Display name for this vehicle (e.g. "Skoda Enyaq") |
| Charger switch | — | Switch to start/stop charging |
| SOC sensor | — | EV state of charge (%) — optional; if absent, the optimizer assumes the vehicle needs charge when the switch is on |
| Target SOC | 80% | Charge EV up to this level |
| Departure time | 07:00 | Used to define the overnight charge window (21:00 → departure) |
| Max charge power | 7400 W | EV charger power limit |
| Capacity | 40 kWh | Battery capacity — used to calculate urgency (hours needed to reach target SOC) |

Use the **+ Add vehicle** button to add more EVs, and **✕** to remove one. Click **Save fleet** to persist the list.

The optimizer decides each EV independently: if a vehicle is connected (charger switch ON or SOC reading present) and its SOC is below the target, it charges when conditions are met (cheap tariff, solar surplus, or overnight window). Multiple EVs can charge simultaneously.

**Migration from < 0.5.5:** if you had a single EV configured, it is automatically converted to a one-entry fleet on the first boot after the upgrade.

#### Forecast & Panel

Required for the 24h cost optimizer to produce a solar-aware schedule. Leave `Panel power` at 0 to disable solar forecasting (the optimizer will still use EPEX prices + consumption history).

| Field | Default | Description |
|-------|---------|-------------|
| Latitude | 0.0 | Decimal latitude of the installation |
| Longitude | 0.0 | Decimal longitude of the installation |
| Panel power | 0 kWp | Peak DC power of the PV system |
| Panel tilt | 35° | Tilt angle: 0 = horizontal, 90 = vertical |
| Panel azimuth | 0° | Azimuth from south: 0 = south, −90 = east, 90 = west |
| Battery capacity | 10 kWh | Usable battery capacity (needed for the 24h schedule) |

Solar forecasts are fetched from [Forecast.Solar](https://forecast.solar) (free public API, no account needed). The forecast is refreshed every 30 minutes alongside the schedule rebuild.

#### Tariff & Optimizer

| Field | Default | Description |
|-------|---------|-------------|
| Price sensor | — | Optional static price sensor (EUR/kWh); overridden by live EPEX when configured |
| Cheap threshold | 0.10 €/kWh | Effective consumption price below which grid charging is triggered |
| Expensive threshold | 0.25 €/kWh | Effective consumption price above which battery discharge is triggered |
| Cheap hysteresis | 0.01 €/kWh | Dead-band above cheap threshold before stopping cheap-mode charging (avoids oscillation) |
| Expensive hysteresis | 0.01 €/kWh | Dead-band below expensive threshold before stopping discharge (avoids oscillation) |
| Look-ahead slots | 4 | Number of cheapest remaining EPEX slots per day to charge in, even if price is above cheap threshold |
| Update interval | 60 s | How often the optimizer runs |
| **Consumption a** | 1.0 | EPEX multiplier for the price you pay when buying from the grid |
| **Consumption b** | 0.0 €/kWh | Fixed offset for the price you pay (grid fees, taxes, margin) |
| **Injection a** | 1.0 | EPEX multiplier for the price you receive when selling to the grid |
| **Injection b** | 0.0 €/kWh | Fixed offset for the price you receive |

**Effective price formula:**
```
Buy price  = tariff_a_consumption × EPEX_raw + tariff_b_consumption
Sell price = tariff_a_injection   × EPEX_raw + tariff_b_injection
```

Example for a Belgian Fluvius dynamic contract (approximate):
- Consumption: `a = 1.0`, `b = 0.07` (distribution + taxes ≈ 7 c€/kWh)
- Injection: `a = 1.0`, `b = -0.02` (EPEX minus prosumer fee)

---

## Optimizer Modes

### AUTO (default)

Rule priority order, evaluated each cycle:

1. **Battery below min SOC** → force charge from grid, pause all EVs
2. **EV urgency** → if an EV must charge now to reach its target SOC before departure (based on SOC gap, capacity, and max charge power), charge it immediately regardless of tariff
3. **24h optimized schedule** → if a day-ahead schedule has been computed (requires panel configuration + EPEX data), follow its charge/discharge recommendation for the current hour
4. **Cheap tariff** (with hysteresis) or **EPEX look-ahead optimal slot** → grid-charge battery and all connected EVs
5. **Solar surplus** (> 200 W deadband) → charge battery first, then all connected EVs
6. **Expensive tariff** (with hysteresis) → discharge battery to cover load, pause EVs
7. **EV overnight window** (21:00 → departure) → charge each EV independently if within its window
8. **Default** → idle

The 24h schedule is rebuilt every 30 minutes. It uses a two-pass greedy algorithm: first classifying hours as solar-surplus (free charge), cheapest-N grid slots (grid charge), or most-expensive deficit hours (discharge); then walking chronologically to ensure battery SOC constraints are respected.

### ECO

Maximises self-consumption, ignores tariff:
- Solar surplus → charge battery, then all connected EVs
- Grid import + battery above min SOC → discharge battery
- Otherwise → idle

### CHEAP

Only acts on tariff:
- Effective buy price ≤ cheap threshold → charge battery and all connected EVs from grid
- Otherwise → idle

### MANUAL

No automatic actions. User controls switches directly via HA.

### OFF

Optimizer does nothing. All switches remain in their last state.

---

## Virtual HA Sensors

The add-on creates these entities on each optimizer cycle. They are available in Lovelace, automations, and the HA energy dashboard.

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.ha_ems_mode` | sensor | Current EMS mode |
| `sensor.ha_ems_battery_decision` | sensor | charge / discharge / standby / idle |
| `sensor.ha_ems_ev_{name}_decision` | sensor | charge / pause — one per vehicle (name is lowercased, spaces→underscores) |
| `sensor.ha_ems_solar_surplus` | sensor (W) | Estimated excess solar available |
| `sensor.ha_ems_reason` | sensor | Human-readable explanation of last decision |
| `sensor.ha_ems_epex_price` | sensor (€/kWh) | Current EPEX slot price with today/tomorrow statistics |

---

## Settings Persistence

Settings are stored in two files under `/data/` (the add-on's persistent volume — survives updates):

| File | Written by | Contains |
|------|-----------|---------|
| `/data/options.json` | Home Assistant | `epex_token`, `epex_zone` |
| `/data/settings.json` | EMS dashboard (on Save) | All other settings |

On startup, the add-on loads both files and immediately writes `/data/settings.json` with the merged result. This means **settings are safe from the first boot** onward.

> **After installing for the first time or upgrading from < 0.5.0:** enter your sensors in the Settings tab and click Save once. They will persist through all future updates.

---

## API Endpoints

The add-on exposes a small REST API on its ingress port (used by the dashboard):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/state` | Current optimizer state (power readings, decisions, prices) |
| GET | `/api/settings` | All settings as JSON |
| POST | `/api/settings` | Update one or more settings |
| POST | `/api/mode` | Change optimizer mode |
| GET | `/api/epex` | Latest EPEX price data (prices_today, prices_tomorrow, stats) |
| GET | `/api/entities` | All HA entities (used to populate sensor picker) |
| GET | `/api/forecast` | 24h battery schedule (per-hour action, solar/consumption forecast, EPEX price) |

---

## Development

The add-on targets `aarch64`, `amd64`, `armhf`, and `armv7`.

To test locally, point `HA_API` in `ha_client.py` at a real HA instance and set `SUPERVISOR_TOKEN` in your environment. The FastAPI app can be run directly with `uvicorn app.main:app`.

Settings during local development are read from `/data/options.json` and `/data/settings.json` — create these files with your test values.
