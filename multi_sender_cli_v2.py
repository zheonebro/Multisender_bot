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

DELAY_SECONDS = float(os.getenv("DELAY_SECONDS", "0.5"))

if not TOKEN_CONTRACT_RAW:
    logger.error("❌ Environment variable 'TOKEN_CONTRACT' tidak ditemukan atau kosong!")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)

# Pilih file CSV saat runtime
def choose_csv_file():
    files = [f for f in os.listdir('.') if f.endswith('.csv')]
    if not files:
        logger.error("❌ Tidak ada file CSV ditemukan di direktori saat ini.")
        exit()

    file_table = Table(title="📂 Pilih file CSV yang berisi wallet address", show_lines=True, box=box.SIMPLE_HEAVY)
    file_table.add_column("No", justify="center")
    file_table.add_column("File Name", style="cyan")

    for idx, fname in enumerate(files, 1):
        file_table.add_row(str(idx), fname)

    console.print(file_table)

    while True:
        try:
            choice = Prompt.ask("Masukkan nomor file yang ingin digunakan", default="1")
            index = int(choice) - 1
            if 0 <= index < len(files):
                return files[index]
            else:
                console.print("[red]Nomor pilihan tidak valid. Coba lagi.[/red]")
        except ValueError:
            console.print("[red]Masukkan angka yang valid![/red]")

CSV_FILE = choose_csv_file()

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
    random.shuffle(valid_addresses)
    return valid_addresses

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
            time.sleep(DELAY_SECONDS)

    panel = Panel("\n".join(logs), title="📤 Hasil Pengiriman", border_style="bright_magenta")
    console.print(panel)

# Menu interaktif

def interactive_menu():
    wallets = load_wallets(CSV_FILE)
    if not wallets:
        console.print("[red]Tidak ada address valid dalam file![/red]")
        return

    min_amt = float(Prompt.ask("💰 Jumlah minimum token", default="1"))
    max_amt = float(Prompt.ask("💰 Jumlah maksimum token", default="5"))
    count = Prompt.ask("📌 Jumlah address untuk dikirim (kosongkan untuk semua)", default="")
    count = int(count) if count.strip().isdigit() else len(wallets)

    selected_wallets = wallets[:count]
    send_batch(selected_wallets, min_amt, max_amt)

if __name__ == "__main__":
    interactive_menu()

