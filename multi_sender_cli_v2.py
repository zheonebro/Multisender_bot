import os
import random
import time
import logging
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from web3 import Web3
import csv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn
from rich.prompt import IntPrompt, Prompt
from rich.text import Text
from rich.table import Table
from rich.live import Live
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore, Lock, Thread

# Setup logging
logging.basicConfig(
    filename="runtime_logs/runtime.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]"
)
logger = logging.getLogger("bot")
console = Console()

# Load config
load_dotenv()
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
SENDER_ADDRESS = Web3.to_checksum_address(os.getenv("SENDER_ADDRESS"))
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("TOKEN_CONTRACT"))
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "2000"))

MIN_TOKEN_AMOUNT = 10.0
MAX_TOKEN_AMOUNT = 50.0
DAILY_WALLET_LIMIT = 200
CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"
TRANSACTION_LOG = f"runtime_logs/transactions_{datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y%m%d_%H%M%S')}.log"

# Connect to Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan!")
    console.print("[bold red]❌ Gagal terhubung ke jaringan![/bold red]")
    exit()

# Token contract
TOKEN_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}]
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}]
    }
]
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=TOKEN_ABI)
TOKEN_DECIMALS = token_contract.functions.decimals().call()

MAX_THREADS = 5
RPC_SEMAPHORE = Semaphore(MAX_THREADS)
nonce_lock = Lock()
global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")


def get_next_nonce():
    global global_nonce
    with nonce_lock:
        current_nonce = global_nonce
        global_nonce += 1
    return current_nonce


def get_transaction_status_by_nonce(nonce):
    try:
        pending_block = w3.eth.get_block('pending', full_transactions=True)
        for tx in pending_block.transactions:
            if tx["from"].lower() == SENDER_ADDRESS.lower() and tx["nonce"] == nonce:
                return "pending"
        return "none"
    except Exception as e:
        logger.error(f"Gagal cek status transaksi nonce {nonce}: {e}")
        return "error"


def get_gas_price(multiplier=5.0, previous=None):
    try:
        gas_price = w3.eth.gas_price / 10**9 * multiplier
        if previous and gas_price <= previous:
            gas_price = previous * 1.5
        return min(gas_price, MAX_GAS_PRICE_GWEI)
    except Exception as e:
        logger.error(f"❌ Gagal ambil gas price: {e}")
        return MAX_GAS_PRICE_GWEI


def cancel_transaction(nonce, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            gas_price = get_gas_price(multiplier=20.0 + (attempt - 1) * 10.0)
            tx = {
                'from': SENDER_ADDRESS,
                'to': SENDER_ADDRESS,
                'value': 0,
                'nonce': nonce,
                'gas': 21000,
                'gasPrice': w3.to_wei(gas_price, 'gwei'),
                'chainId': w3.eth.chain_id
            }
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            console.print(f"[yellow]🚫 Membatalkan nonce {nonce} (attempt {attempt}): {tx_hash.hex()[:10]}...[/yellow]")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return tx_hash
        except Exception as e:
            if "already known" in str(e):
                return None
            logger.error(f"Gagal membatalkan nonce {nonce} (attempt {attempt}): {e}")
            time.sleep(2)
    return None


def countdown_timer():
    tz = pytz.timezone('Asia/Jakarta')
    now = datetime.now(tz)
    next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_left = int((next_run - now).total_seconds())

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]⏳ Menunggu pengiriman berikutnya:"),
        BarColumn(),
        TimeRemainingColumn(),
        TextColumn("[bold yellow]{task.completed} detik tersisa[/bold yellow]"),
        transient=True,
    ) as progress:
        task = progress.add_task("Menunggu...", total=seconds_left)
        while seconds_left > 0:
            time.sleep(1)
            seconds_left -= 1
            progress.update(task, advance=1)

    console.print("\n[bold green]🕛 Mulai ulang pengiriman token![/bold green]")
    main()


def show_intro():
    console.rule("[bold cyan]ERC-20 Token Sender")
    intro = "🚀 Mengirim token secara otomatis ke 200 wallet setiap hari."
    text = Text("", style="bold magenta")
    with Live(text, refresh_per_second=20) as live:
        for char in intro:
            text.append(char)
            live.update(text)
            time.sleep(0.03)
    console.print("\n")


def show_status_info():
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / 10**TOKEN_DECIMALS
    sent_today = len(open(SENT_FILE).readlines())
    table = Table(title="📊 Status Wallet")
    table.add_column("Item", style="cyan", no_wrap=True)
    table.add_column("Detail", style="magenta")
    table.add_row("💼 Wallet", f"{SENDER_ADDRESS[:10]}...")
    table.add_row("💰 Sisa Token", f"{balance:,.2f}")
    table.add_row("📤 Terkirim Hari Ini", f"{sent_today}/{DAILY_WALLET_LIMIT}")
    console.print(table)


def main():
    show_intro()
    show_status_info()
    selection_mode = Prompt.ask("Pilih metode pengambilan wallet", choices=["random", "sequential"], default="random")
    wallets = load_wallets(selection_mode)
    console.print(f"[blue]📦 Jumlah wallet yang akan dikirim: {len(wallets)}[/blue]")

    if not wallets:
        console.print("[yellow]📭 Semua wallet sudah dikirimi token hari ini.[/yellow]")
        countdown_timer()
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeRemainingColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("Mengirim token...", total=len(wallets))
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = [executor.submit(send_worker, wallet) for wallet in wallets]
            for future in as_completed(futures):
                progress.advance(task)

    console.print(Panel("[bold green]🎉 Pengiriman selesai![/bold green]"))
    countdown_timer()


if __name__ == "__main__":
    main()
