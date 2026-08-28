# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- Sovereignty section: hub fuel with configurable bands, ADM board, a
  three-state timer board (safe / attackable / reinforced), and industry cost
  indices for every system the alliance holds.
- Skyhook section: stealable skyhooks only, per-reagent theft thresholds,
  reinforcement timers, and the public raid target list with real planet names.
- Mercenary den section: den sites derived from temperate-planet skyhooks, a
  claim and approval workflow, an anonymous availability list, an admin view
  with contact details, and tactical operation tracking.
- Workforce siphon detection. An untouched skyhook reports a workforce figure
  that is a round multiple of ten; a den takes a percentage and leaves one that
  usually is not. This needs no history, so it catches dens that were already
  in place before the app was installed.
- Discord alerting with per-category routing, so fuel warnings and "something
  is being shot" can go to different channels.
- Alliance Auth notifications for anything addressed to one person: a decided
  den claim, your own den being attacked or reinforced.
- Sidebar badges showing how many things in each section want attention.
- charlink integration, alongside the built-in token registration page.

### Notes

- Requires ESI compatibility date 2026-05-19 or later. The sovereignty hub and
  skyhook routes do not exist before it, and ESI answers 404 rather than
  telling you why.
