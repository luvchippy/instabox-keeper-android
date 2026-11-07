# instabox-keeper-android

Keeps a free-tier InstaBox machine running continuously from GitHub Actions.

## Why

A free-tier machine pauses itself after 30 idle minutes, and hitting its
`maxRunMinutes` triggers a punitive cooldown (5-20 / 60-75 / 120-135 minutes).
A manually suspended machine, however, owes no cooldown, and `resume` resets
`runStartedAt`.

So this workflow runs every 10 minutes and:

1. sends a real `exec` keepalive so the idle timer never fires;
2. once `runStartedAt` is within `maxRunMinutes - 20`, breathes
   (`suspend` -> `resume`), which zeroes the timer without owing a cooldown.

The machine therefore never hits max-run and never enters the punitive cooldown.
Each breath freezes the guest for a few seconds; memory and processes are kept.

## Setup

1. Create a repository secret `INSTABOX_API_KEY` (needs `machines:read` and
   `machines:write`).
2. Create a repository secret `INSTABOX_MACHINE` (the machine id `m_...` or
   its name).
3. Run the workflow once by hand (**Actions -> instabox-keeper -> Run workflow**).

## Notes

- Keep this repository public: scheduled workflow minutes are free there.
- GitHub cron is best-effort and may be delayed by a few minutes; the 30 minute
  idle window absorbs that.
- The keeper must run outside the machine: a suspended guest cannot wake itself.
