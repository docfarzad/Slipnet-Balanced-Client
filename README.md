# Slipnet Balanced Client
[ [English](https://github.com/docfarzad/Slipnet-Balanced-Client/tree/main) |‌ [فارسی / Persian](https://github.com/docfarzad/Slipnet-Balanced-Client/blob/main/README_fa.md) ]

Simple desktop tool to find working DNS resolvers for `slipnet.exe`, then run a local balanced proxy pool.

## What it does

- Loads a text file of resolver IPs (one per line). Repeated lines are deduplicated automatically. 
- Scans them in parallel and keeps only working ones.
- Lets you pick which good resolvers to activate.
- Starts local proxy servers:
- `SOCKS5` on `0.0.0.0:1080`
- `HTTP` on `0.0.0.0:8080`
- Routes traffic through the active resolver pool with basic load balancing and failure cooldown.

## Requirements

- Windows (the project expects `slipnet.exe`, downloadable from [here](https://github.com/anonvector/SlipNet/releases/)).
- Python 3.9+ recommended.
- Python package: `requests`
- `slipnet.exe` in the same folder as `Slipnet Balanced Client.py`

Install dependency:

```bash
pip install requests
```

## Files

- `Slipnet Balanced Client.py` - GUI app and proxy logic.
- `slipnet.exe` - backend executable used for resolver tunneling.

## How to use

1. Run the app:

```bash
python "Slipnet Balanced Client.py"
```

2. In the UI, click **Browse IP List** and choose your resolver list file.
3. Enter your **Slipnet connection string**.
4. Set **Workers** (parallel scan count), then click **Start Scan**.
5. During or after the scan completes, select good resolvers in the table (`[ ]` / `[x]` column).
6. Click **Activate Selected** to start the pool.
7. Use the shown local proxy addresses in your browser/app:
- SOCKS5: `your_local_ip:1080`
- HTTP: `your_local_ip:8080`
8. Optional: click **Verify Good Resolvers' Quality** to stress-test and mark unstable entries.

## Resolver list format

- Plain text file.
- One resolver per line.
- Empty lines are ignored.
- Duplicate lines are removed automatically.

Example:

```txt
1.1.1.1
8.8.8.8
9.9.9.9
```

## Behavior notes

- Scan results show latency and status (`OK` / `FAIL` / `PENDING`).
- Failed resolvers are marked in real time, not automatically deleted.
- The balancer tracks failures and temporarily cools down unstable resolvers, removing the need for constant adjustment. 
- Recovered resolvers are gradually reintroduced (slow start).
- Existing active backends are stopped cleanly when the app closes.

## Advantages

- Easy GUI workflow (no manual command chaining).
- Fast parallel scanning for large resolver lists.
- Supports both SOCKS5 and HTTP clients outside the machine on the same network.
- Better stability from failure tracking + cooldown logic.
- Lets you build a custom active pool instead of using all “good” resolvers blindly.

## Build locally from source

1. Switch to repository directory
2. Build the app:

```bash
pyinstaller --onefile --noconsole --name "Slipnet Balanced Client" --add-binary "slipnet.exe;." "Slipnet Balanced Client.py"
```

## Troubleshooting

- **"Executable not found"**: make sure `slipnet.exe` is next to `Slipnet Balanced Client.py`.
- **No good resolvers found**: check your list quality and Slipnet connection string.
- **Proxy not working in another device**: ensure firewall/network allows ports `1080` and `8080`.
- **Scan is slow**: reduce worker count if your system/network is overloaded, or increase it for faster testing (with caution).

## Disclaimer

Use only on networks/systems where you have permission. You are responsible for compliant and legal usage.
