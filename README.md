# Dashy

LAN-accessible Cider dashboard for music playback, lyrics, and system stats.

## Features

- Music view with album art, controls, and synced lyrics
- Stats view for CPU, GPU, RAM, disk, and temperatures
- Lyrics caching in memory and on disk
- Local-network access via `http://<hostname>.local:5000/`
- `systemd --user` packaging for autostart
- Optional suspend/wake helper for phone + PC sleep workflow

## Requirements

- Linux
- `python3`
- `systemd`
- `adb` if you use the phone sleep/wake helper
- Avahi/mDNS if you want `hostname.local`

Python dependencies are installed automatically into a dedicated virtualenv by `install.sh`.

## Install

```bash
./install.sh
```

This installs or updates Dashy and restarts the user service.

Default URL:

```bash
http://$(hostname).local:5000/
```

If Avahi renames your host due to an mDNS conflict, the live hostname may be something like `hostname-2.local` instead.

## Update

Re-run:

```bash
./install.sh
```

This:

- copies the latest app files into the install directory
- refreshes the virtualenv dependencies
- rewrites the `systemd` unit
- reloads `systemd`
- restarts Dashy
- updates the optional root-level wake service and helper

## Uninstall

```bash
./uninstall.sh
```

This removes:

- the user service
- the installed app directory
- the lyrics cache
- the root-level post-wake service
- the root-level sleep helper

## Installed Paths

App files:

- `~/.local/share/dashy/server.py`
- `~/.local/share/dashy/index.html`
- `~/.local/share/dashy/static/styles.css`
- `~/.local/share/dashy/static/app.js`
- `~/.local/share/dashy/scripts/dashy-sleep-pc.sh`
- `~/.local/share/dashy/.venv/`

Cache:

- `~/.cache/dashy/`
- `~/.cache/dashy/lyrics/`

User service:

- `~/.config/systemd/user/dashy.service`

Root-level helper and service:

- `/usr/local/bin/dashy-sleep-pc`
- `/etc/systemd/system/phone-post-wake.service`

## Services Created

### `dashy.service`

User-level service managed with `systemctl --user`.

Purpose:

- runs the Flask server
- binds to `0.0.0.0:5000`
- starts automatically with your user session

Important settings:

- `Nice=10`
- `IOSchedulingClass=idle`
- `IOSchedulingPriority=7`
- `CPUWeight=1`

Those lower its scheduling priority so it stays out of the way while gaming.

Useful commands:

```bash
systemctl --user status dashy --no-pager
systemctl --user restart dashy
journalctl --user -u dashy -n 100 --no-pager
systemctl --user show dashy -p Nice -p IOSchedulingClass -p IOSchedulingPriority -p CPUWeight
```

Check the process niceness:

```bash
ps -o pid,ni,comm -C python
```

### `phone-post-wake.service`

System-level service managed with `sudo systemctl`.

Purpose:

- runs after resume from suspend
- reconnects to the configured Android phone over ADB
- sends keyevent `224` to wake the phone

The generated unit currently hardcodes:

- phone target: `192.168.0.8:5555`

Useful commands:

```bash
sudo systemctl status phone-post-wake.service
sudo systemctl restart phone-post-wake.service
sudo journalctl -u phone-post-wake.service -n 100 --no-pager
```

## Sleep Helper

Installed helper:

```bash
/usr/local/bin/dashy-sleep-pc
```

Normal mode:

- connects to the phone over ADB
- sends keyevent `223` to put the phone to sleep
- waits briefly
- suspends the PC

Wake-only mode:

```bash
/usr/local/bin/dashy-sleep-pc --wake-only
```

This:

- retries ADB connection/wake a few times
- sends keyevent `224`
- exits successfully once the phone wakes

## Notes

- `dashy.service` is a user service, so `sudo systemctl status dashy` will not work.
- Use `systemctl --user ...` for Dashy.
- Use `sudo systemctl ...` for `phone-post-wake.service`.
- For true boot-before-login behavior for Dashy, enable linger:

```bash
loginctl enable-linger "$USER"
```

- If `.local` does not resolve, test with your machine IP and verify Avahi/mDNS on the host and the client device.

## Repo Layout

- [server.py](/home/aditya/Documents/cider-test/server.py)
- [index.html](/home/aditya/Documents/cider-test/index.html)
- [styles.css](/home/aditya/Documents/cider-test/static/styles.css)
- [app.js](/home/aditya/Documents/cider-test/static/app.js)
- [install.sh](/home/aditya/Documents/cider-test/install.sh)
- [uninstall.sh](/home/aditya/Documents/cider-test/uninstall.sh)
- [dashy-sleep-pc.sh](/home/aditya/Documents/cider-test/scripts/dashy-sleep-pc.sh)
