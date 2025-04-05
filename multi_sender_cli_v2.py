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
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.align import Align
from rich.logging import RichHandler
from rich.live import Live
import web3
import schedule
from web3.exceptions import TransactionNotFound

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
console.print(Panel.fit(BANNER, title="[bold green]🚀 ERC20 Sender Bot[/bold green]", border_style="cyan"))

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

        logger.info(f"[bold green]📊 Token balance:[/bold green] {token_balance:.4f} {TOKEN_NAME}")
        logger.info(f"[bold yellow]⛽ TEA balance (untuk gas):[/bold yellow] {eth_balance:.6f} TEA")
        logger.info(f"[cyan]🔗 Estimasi TX sisa:[/cyan] {estimated_tx_possible} transaksi")
    except Exception as e:
        logger.error(f"[red]❌ Gagal membaca balance: {e}[/red]")

# Fungsi tampilkan log online berjalan tanpa tabel, dengan format lebih menarik

def show_log_live():
    log_file = os.path.join("runtime_logs", "runtime.log")
    if not os.path.exists(log_file):
        console.print("[red]Log file tidak ditemukan.[/red]")
        return

    with Live(console=console, refresh_per_second=1):
        try:
            while True:
                with open(log_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()[-20:]
                console.clear()
                for line in lines:
                    try:
                        timestamp, msg = line.strip().split(" ", 1)
                        # Format log dengan warna dan emoji untuk meningkatkan daya tarik
                        if "✅" in msg:
                            console.print(f"[green]{timestamp}[/green] [bold green]✅ {msg}[/bold green]")
                        elif "⚠️" in msg:
                            console.print(f"[yellow]{timestamp}[/yellow] [bold yellow]⚠️ {msg}[/bold yellow]")
                        elif "❌" in msg:
                            console.print(f"[red]{timestamp}[/red] [bold red]❌ {msg}[/bold red]")
                        else:
                            console.print(f"[dim]{timestamp}[/dim] {msg}")
                    except:
                        continue
                time.sleep(3)
        except KeyboardInterrupt:
            return

# Fungsi check RPC limit

def check_rpc_limit():
    try:
        w3.eth.get_block('latest')
        return True
    except web3.exceptions.BlockNotFound:
        logger.warning("⚠️ Terkena limit RPC, menunggu 60 detik...")
        time.sleep(60)
        return False

# Fungsi kirim token acak dengan adaptive delay

def send_tokens():
    if not wallets_all:
        logger.warning("❗ Daftar wallet kosong.")
        return

    random.shuffle(wallets_all)
    total = len(wallets_all)
    logger.info(f"🚀 Mulai pengiriman batch ke {total} wallet...")

    for i, to_address in enumerate(wallets_all):
        try:
            # Generate token amount between 10 and 100
            amount = random.randint(10, 100) * (10 ** decimals)
            nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)

            # Check RPC limit before proceeding
            if not check_rpc_limit():
                continue

            tx = token_contract.functions.transfer(to_address, amount).build_transaction({
                'from': SENDER_ADDRESS,
                'nonce': nonce,
                'gas': 60000,
                'gasPrice': w3.eth.gas_price
            })
            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            logger.info(f"✅ Tx terkirim ke {to_address} | Hash: {tx_hash.hex()}")
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
        except Exception as e:
            if "too many requests" in str(e).lower():
                logger.warning("⚠️ Terkena limit RPC. Menunggu 60 detik...")
                time.sleep(60)
            else:
                logger.error(f"❌ Gagal kirim ke {to_address}: {e}")

# Penjadwalan pengiriman token setiap jam

def start_scheduler():
    schedule.every().hour.do(send_tokens)
    logger.info("⏰ Penjadwalan pengiriman token setiap jam telah dimulai.")
    while True:
        schedule.run_pending()
        time.sleep(1)

# Main function

if __name__ == "__main__":
    log_balances()
    threading.Thread(target=start_scheduler, daemon=True).start()
    show_log_live()
