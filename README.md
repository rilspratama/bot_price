# Crypto Price Telegram Bot

A Telegram bot for cryptocurrency prices, conversions, DEX pool lookup, wallet balance fallback, gas tracking, and simple math calculations.

## Features

- `/price BTC` checks coin prices from CoinGecko with GeckoTerminal fallback.
- `/list MON` shows CoinGecko search candidates.
- `/gas` opens an inline chain picker for gas tracking.
- `/gas base`, `/gas eth`, `/gas bnb`, and other supported chains show gas directly.
- Auto replies for explicit formats such as `1 eth`, `1 eth idr`, `10000 idr eth`, and `2 * 2`.
- EVM and Solana address detection:
  - First tries pool/token lookup through GeckoTerminal.
  - Falls back to native wallet balance through public RPC if no pool is found.
- Inline buttons for `Delete`, `List`, `Dex`, and gas chain selection.
- Cache, retry, provider cooldowns, and shared HTTP clients to reduce rate-limit issues.

## Requirements

- Python 3.11+ recommended.
- Git.
- A Linux VPS or local machine.
- A Telegram bot token from BotFather.
- Telegram API ID and API Hash.

## Environment Setup

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token
RPC_BALANCE_CHAINS=
PYROGRAM_WORKDIR=.
```

`RPC_BALANCE_CHAINS` is optional. Leave it empty to check all default EVM chains for wallet balance fallback.

`PYROGRAM_WORKDIR` controls where Pyrogram stores its session file. Docker Compose overrides it to `/app/data` so the session can persist in a Docker volume.

Example:

```env
RPC_BALANCE_CHAINS=Ethereum,Base,BNB
```

## Run with Docker Compose

Clone the repository:

```bash
git clone https://github.com/rilspratama/bot_price.git
cd bot_price
```

Create and edit `.env`:

```bash
cp .env.example .env
nano .env
```

Start the bot:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f
```

Stop the bot:

```bash
docker compose down
```

Update after pulling new code:

```bash
git pull
docker compose up -d --build
```

## Run with Python Virtual Environment

Clone the repository:

```bash
git clone https://github.com/rilspratama/bot_price.git
cd bot_price
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

Create and edit `.env`:

```bash
cp .env.example .env
nano .env
```

Run the bot:

```bash
python3 telegram_bot.py
```

## Run as a systemd Service

Copy the service template:

```bash
sudo cp crypto-price-bot.service.example /etc/systemd/system/crypto-price-bot.service
```

Edit the `User`, `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` values for your VPS:

```bash
sudo nano /etc/systemd/system/crypto-price-bot.service
```

Reload systemd and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-price-bot
```

Check status and logs:

```bash
sudo systemctl status crypto-price-bot
sudo journalctl -u crypto-price-bot -f
```

Restart after code updates:

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart crypto-price-bot
```

## Bot Commands

```text
/start
/price BTC
/list MON
/gas
/gas eth
/gas base
1 eth
1 eth idr
10000 idr eth
2 * 2
```

## Optional CLI Usage

The CoinGecko price checker can also be used from the command line:

```bash
python3 main.py BTC
python3 main.py MON --list
python3 main.py bitcoin --id
```

## Supported Public Providers

This bot uses free public endpoints:

- CoinGecko for coin prices.
- GeckoTerminal for DEX pool fallback.
- Public RPC endpoints for wallet balances and gas tracking.
- A public currency API for fiat conversions.

Public endpoints may rate-limit or timeout. The bot includes caching, retries, and provider cooldowns to reduce this risk.