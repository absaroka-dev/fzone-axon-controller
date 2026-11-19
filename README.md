# FZONE AXON Local Controller

An unofficial, local-only Bluetooth Low Energy controller for the FZONE AXON
WRGB aquarium light. It provides:

- read/write control of global power and eight channel values;
- a Unix-domain-socket API with a deliberately small command allowlist;
- bounded temporary manual overrides;
- fixed-duration solar-noon scheduling with smooth ramps;
- seasonal and daylight-saving-time handling without extending the aquarium
  photoperiod;
- optional acclimation and advisory-only CO2 timing; and
- an optional Homebridge color-light accessory.

The implementation was developed and tested against one AXON 72 W freshwater
pendant. Other hardware or firmware revisions may behave differently. FZONE is
not affiliated with or responsible for this project.

## Safety and aquarium scope

This software controls lighting only. Keep filtration, circulation, heating,
aeration, dosing, and other life-support equipment independent. Channel
percentages are not PAR, DLI, or proof of biological suitability. Begin with a
conservative schedule and use plant response, algae response, stable CO2, and
preferably PAR measurements when tuning it.

The controller defaults to read-only unless `--allow-control` or
`AXON_ALLOW_CONTROL=1` is explicitly set. Schedule examples are disabled.

## Requirements

- Linux with BlueZ and Python 3.11 or newer
- a BLE adapter visible to BlueZ
- the light's BLE address

## Install and run

```bash
python3 -m venv venv
venv/bin/pip install .
AXON_BLE_ADDRESS=AA:BB:CC:DD:EE:FF \
  AXON_ALLOW_CONTROL=1 \
  AXON_SCHEDULE=/etc/axon-light/schedule.json \
  venv/bin/axon-light-controller
```

In another shell, using an account allowed to access the socket:

```bash
venv/bin/axon-light-client health
venv/bin/axon-light-client status
```

Copy `config/schedule.example.json` for a fixed schedule or
`config/solar-schedule.example.json` for a geographically anchored schedule.
Both are disabled until explicitly enabled. The solar example contains neutral
placeholder coordinates and must be configured for the installation location.

The generic `systemd/axon-light.service` reads site-specific settings from
`/etc/axon-light/axon-light.env`; never commit the real environment file.

## Homebridge

The `homebridge/` directory contains a native HomeKit color-light accessory.
It talks to `axon-light-client` through a restricted SSH account. Its SSH
identity, known-hosts file, controller address, and user remain deployment
configuration and are not included here.

## Development

```bash
python3 -m unittest discover -s tests
cd homebridge && npm test
```

## Protocol notes

The protocol helpers keep a conventional W/R/G/B public order and translate
to the observed device order internally. Channels 5–8 are retained but were
unused on the tested fixture. All frame parsing validates header, length,
checksum, and channel ranges.

## Versioning and downstream use

Downstream projects should pin a release tag or commit. Do not copy and edit a
second controller implementation: contribute changes here, release them, and
then update the downstream pin. This repository uses semantic versioning.

