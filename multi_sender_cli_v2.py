import csv
import os
import random
import time
from datetime import datetime
import traceback
import threading
import logging

from dotenv import load_dotenv
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.align import Align
from rich.logging import RichHandler
from rich.layout import Layout
from rich import box
import web3
import schedule

# Init
console = Console()
load_dotenv()

# Banner
BANNER = """
███████╗██████╗  ██████╗  ██████╗ ██████╗  ██████╗ ███╗   ██╗
██╔════╝██╔══██╗██╔═══██╗██╔════╝ ██╔══██╗██╔═══██╗████╗  ██║
█████╗  ██████╔╝██║   ██║██║  ███╗██████╔╝██║   ██║██╔██╗ ██║
██╔══╝  ██╔══██╗██║   ██║██║   ██║██╔═══╝ ██║   ██║██║╚██╗██║
███████╗██║  ██║╚██████╔╝╚██████╔╝██║     ╚██████╔╝██║ ╚████║
╚══════╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚═╝  ╚═══╝
"""
console.print(Panel.fit(BANNER, title="[bold green]🚀 ERC20 Sender Bot[/bold green]", border_style="cyan", box=box.DOUBLE))

# Setup logging
log_dir = "runtime_logs"
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "runtime.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]",
    handlers=[
        RichHandler(console=console, markup=True),
        logging.FileHandler(log_path, encoding="utf-8")
    ]
)
logger = logging.getLogger("bot")

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RAW_SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_RAW = os.getenv("TOKEN_CONTRACT")
EXPLORER_URL = "https://sepolia.tea.xyz/"

if not PRIVATE_KEY or not RAW_SENDER_ADDRESS or not RPC_URL:
    logger.error("❌ PRIVATE_KEY, SENDER_ADDRESS, atau INFURA_URL tidak ditemukan di .env!")
    exit()

SENDER_ADDRESS = web3.Web3.to_checksum_address(RAW_SENDER_ADDRESS)

DAILY_LIMIT_RAW = os.getenv("DAILY_LIMIT", "0")
try:
    DAILY_LIMIT = float(DAILY_LIMIT_RAW)
except ValueError:
    DAILY_LIMIT = 0

MIN_DELAY_SECONDS = float(os.getenv("MIN_DELAY_SECONDS", "0.5"))
MAX_DELAY_SECONDS = float(os.getenv("MAX_DELAY_SECONDS", "2"))

if not TOKEN_CONTRACT_RAW:
    logger.error("❌ Environment variable 'TOKEN_CONTRACT' tidak ditemukan atau kosong!")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)

CSV_FILE = "wallets.csv"  # default file, tidak ada prompt lagi

# Connect Web3
w3 = web3.Web3(web3.Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan! Cek RPC URL")
    exit()

# ERC20 ABI
ERC20_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_to", "type": "address"},
            {"internalType": "uint256", "name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "name",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Contract
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
decimals = token_contract.functions.decimals().call()
TOKEN_NAME = token_contract.functions.name().call()

nonce_lock = threading.Lock()
daily_sent_total = 0.0
daily_lock = threading.Lock()

# Fungsi load wallet

def load_wallets(csv_file):
    valid_addresses = []
    with open(csv_file, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                address = web3.Web3.to_checksum_address(row['address'].strip())
                valid_addresses.append(address)
            except:
                continue
    return valid_addresses

wallets_all = load_wallets(CSV_FILE)

# Fungsi monitoring balance dan TEA (ETH) untuk gas

def log_balances():
    try:
        token_balance_raw = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
        token_balance = token_balance_raw / (10 ** decimals)

        eth_balance_wei = w3.eth.get_balance(SENDER_ADDRESS)
        eth_balance = w3.from_wei(eth_balance_wei, 'ether')

        gas_price = w3.eth.gas_price
        estimated_gas_per_tx = 50000
        estimated_tx_possible = int(eth_balance_wei / (estimated_gas_per_tx * gas_price))

        logger.info(f"📊 [bold]Token balance:[/bold] {token_balance:.4f} {TOKEN_NAME}")
        logger.info(f"⛽ [bold]TEA balance (untuk gas):[/bold] {eth_balance:.6f} TEA")
        logger.info(f"🔢 [bold]Estimasi TX sisa:[/bold] {estimated_tx_possible} transaksi")
    except Exception as e:
        logger.error(f"[red]Gagal membaca balance: {e}[/red]")

# Fungsi tampilkan log online

def show_log():
    log_file = os.path.join("runtime_logs", "runtime.log")
    if not os.path.exists(log_file):
        console.print("[red]Log file tidak ditemukan.[/red]")
        return

    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()[-20:]  # tampilkan 20 log terakhir

    table = Table(title="📜 Log Terbaru", show_header=True, header_style="bold cyan")
    table.add_column("Waktu", style="dim")
    table.add_column("Pesan")

    for line in lines:
        try:
            timestamp, msg = line.strip().split(" ", 1)
            table.add_row(timestamp, msg)
        except:
            continue

    console.print(table)

# Fungsi kirim token

def send_token(to_address, amount):
    to_address = web3.Web3.to_checksum_address(to_address)
    with nonce_lock:
        nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)

    tx_func = token_contract.functions.transfer(to_address, int(amount * (10 ** decimals)))
    gas_estimate = tx_func.estimate_gas({'from': SENDER_ADDRESS})
    gas_price = w3.eth.gas_price

    tx = tx_func.build_transaction({
        'chainId': w3.eth.chain_id,
        'gas': gas_estimate,
        'gasPrice': gas_price,
        'nonce': nonce
    })

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    return w3.to_hex(tx_hash)

# Kirim batch

def send_batch(wallets, min_amt, max_amt):
    global daily_sent_total
    logs = []
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        transient=True,
        console=console
    ) as progress:
        task = progress.add_task("Mengirim token...", total=len(wallets))

        for i, wallet in enumerate(wallets, 1):
            amount = round(random.uniform(min_amt, max_amt), 6)

            with daily_lock:
                if DAILY_LIMIT and daily_sent_total + amount > DAILY_LIMIT:
                    logger.warning("⚠️ Batas harian tercapai.")
                    break
                daily_sent_total += amount

            try:
                tx_hash = send_token(wallet, amount)
                log = f"[green]{i}. {wallet} ✅ {amount} {TOKEN_NAME} | TX: {tx_hash}[/green]"
                logger.info(log)
            except Exception as e:
                log = f"[red]{i}. {wallet} ❌ Gagal: {e}[/red]"
                logger.error(log)

            logs.append(log)
            progress.update(task, advance=1)
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

    panel = Panel("\n".join(logs), title="📤 Hasil Pengiriman", border_style="bright_magenta")
    console.print(panel)

# Penjadwalan otomatis tiap jam

def scheduled_send():
    if not wallets_all:
        logger.warning("📛 Tidak ada address untuk dikirim.")
        return

    log_balances()

    batch_size = max(1, len(wallets_all) // 24)
    selected_wallets = random.sample(wallets_all, batch_size)
    logger.info(f"⏰ Menjalankan pengiriman batch otomatis ke {batch_size} address...")
    send_batch(selected_wallets, 1.0, 5.0)

schedule.every().hour.at(":00").do(scheduled_send)

# Thread untuk scheduler

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(5)

threading.Thread(target=run_scheduler, daemon=True).start()

# Menu interaktif (opsional manual)

def interactive_menu():
    console.print("[cyan]Bot aktif. Pengiriman otomatis dijadwalkan setiap jam.[/cyan]")
    while True:
        action = Prompt.ask("\n[bold yellow]Perintah[/bold yellow] ([green]log[/green]/[red]exit[/red])", default="exit")
        if action == "log":
            show_log()
        elif action == "exit":
            break

if __name__ == "__main__":
    interactive_menu()
