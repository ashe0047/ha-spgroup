# SP Group

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant custom integration for SP Group (Singapore Power) utility data.
Track your Singapore electricity and water consumption directly in Home
Assistant, with Home Energy Dashboard support and historical data backfill.

---

## Features

- **Electricity consumption sensor** — Near real-time (30-minute intervals)
- **Water consumption sensor** — Daily and monthly buckets
- **Home Energy Dashboard ready** — Cumulative totals with manual meter-reading
  seed for accurate baselines
- **Automated polling** — Electricity every hour (configurable), water daily
- **Historical data backfill** — Import past consumption data on setup
- **OTP-authenticated login** — Uses the same credentials as the SP Group app
- **Utility discovery** — Auto-detects your electricity and water accounts
- **Accumulation ledger** — Persistent state across HA restarts

---

## Prerequisites

- An [SP Group](https://www.spgroup.com.sg/) account with registered utility
  accounts (electricity and/or water)
- Home Assistant 2026.7.2 or newer
- [HACS](https://hacs.xyz/) installed (recommended) or manual installation

---

## Installation

### Via HACS (recommended)

1. In HACS, click **Explore & Add Repositories**
2. Add this repository as a custom repository:
   - URL: `https://github.com/ashe0047/ha-spgroup`
   - Category: **Integration**
3. Click **Install** on the SP Group integration card
4. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/ashe0047/ha-spgroup/releases)
2. Extract `custom_components/sp_group/` into your Home Assistant
   `custom_components/` directory
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**
2. Search for **SP Group** and select it
3. Enter your SP Group account email and password
4. Complete the one-time password (OTP) flow — the code is sent to your
   registered email or phone
5. Select the utilities you want to monitor (electricity, water, or both)
6. For each selected utility, enter a **physical meter reading**:
   - Time: Set to when you read the meter
   - Reading: Enter the cumulative value shown on your meter
   - Backfill from: Choose how far back to import historical data (SP Group
     API provides up to 5 years for electricity, less for water)
7. Continue through the seed confirmation steps

After setup, sensors appear as `sensor.sp_group_electricity_consumption` and
`sensor.sp_group_water_consumption`.

### Options

Go to **Settings > Devices & Services > SP Group > Configure** to:

- Enable/disable utilities
- Adjust electricity polling interval (default: 1 hour)
- Set water polling day and time (default: day 1 at midnight SGT)
- Run maintenance actions (reseed, rebuild ledger, retry backfill)

---

## Sensors

| Sensor | Unit | Device Class | State Class |
|--------|------|--------------|-------------|
| Electricity consumption | kWh | energy | total_increasing |
| Water consumption | m³ | water | total_increasing |

Both sensors are energy dashboard-ready after a manual meter-reading seed is
provided.

---

## FAQ / Troubleshooting

**Q: Do I need an SP Group account?**
You need an existing SP Group account with registered electricity and/or water
utility accounts. The integration uses the same credentials as the SP Mobile
app.

**Q: What data does the integration access?**
The integration reads consumption data from SP Group's mobile API endpoints.
It does not access billing details, payment information, or personal data
beyond what's needed to identify your utility accounts.

**Q: How is the seed reading used?**
The seed reading establishes a baseline so the integration can accumulate
consumption from bucket data returned by the SP Group API. Without a seed,
sensors show as unavailable.

**Q: What happens if the seed reading is approximate?**
If the observation timestamp doesn't align to SP Group's bucket boundary
(e.g. a random time instead of HH:00 or HH:30 for electricity), the
integration will start accumulation at the next clean boundary and may skip
the partial interval.

**Q: Can I replace the meter seed?**
Yes. Go to the integration options and use the reseed action. You can also
rebuild the accumulation ledger if needed.

---

## Support

- Report issues on the [issue tracker](https://github.com/ashe0047/ha-spgroup/issues)
- For development instructions, see [CONTRIBUTING.md](CONTRIBUTING.md)

---

## License

MIT
