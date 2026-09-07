# Switchfin device sweep — openclaw-home deployment (2026-09-06)

Archived copy of the runner report (`docs/switchfin-test-report.md` is
gitignored — this is the tracked record; see `device-driving.md` Run #20
for the findings around it).

- Date: 2026-09-06
- Host: openclaw-home (192.168.2.223) — BitPlay engine via
  `deploy/docker-compose.bitplay.yml`, backend on `:8003` from merged
  master (29b553c)
- Phone: OnePlus 8 Pro (IN2023), Android 14, wireless debugging
- Switchfin server retargeted: `.166:8003` → `.223:8003`
- Verification: ✅ verified on device

## Handshake

- login: ✅
- views: ✅

## Warmup

- warmup_recent_movie: ✅
- warmup_recent_series: ✅
- warmup_popular: ✅
- warmup_movie: ✅
- warmup_series: ✅
- warmup_anime: ✅
- warmup_cartoon: ✅
- warmup_dorama: ✅

## Device

- restart_app: ✅
- back_to_grid_after_recent_movie: ✅ — reached Views grid after 5 BACK(s)
- back_to_grid_after_recent_series: ✅ — reached Views grid after 6 BACK(s)
- back_to_grid_after_popular: ✅ — reached Views grid after 5 BACK(s)
- back_to_grid_after_movie: ✅ — reached Views grid after 6 BACK(s)
- back_to_grid_after_series: ✅ — reached Views grid after 5 BACK(s)
- back_to_grid_after_anime: ✅ — reached Views grid after 6 BACK(s)
- back_to_grid_after_cartoon: ✅ — reached Views grid after 6 BACK(s)
- back_to_grid_after_dorama: ✅ — reached Views grid after 4 BACK(s)

## View sweep

| View | Open | Detail | Play |
|---|---|---|---|
| Нещодавно додані: Фільми | ✅ | ✅ | ✅ |
| Нещодавно додані: Серіали | ✅ | ✅ | ✅ |
| Популярні зараз | ✅ | ✅ | ✅ |
| Фільми | ✅ | ✅ | ✅ |
| Серіали | ✅ | ✅ | ✅ |
| Аніме | ✅ | ✅ | ✅ |
| Мультфільми | ✅ | ✅ | ✅ |
| Дорами | ✅ | ✅ | ❌ |

## Notes

- `play_dorama` ❌ — first_season: timeout (retried); first_episode:
  timeout (retried). Classified post-run (see device-driving.md Run #20):
  backend answered the dorama's `Seasons` in 4ms during the battery —
  the B8/B22 tap-drift class, not a backend defect.

## Verdict

**FAIL ❌** — 42 passed, 0 skipped, 1 failed
