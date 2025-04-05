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
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.logging import RichHandler
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich import box
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

        logger.info(f"[blue]📊 Token balance: {token_balance:.4f} {TOKEN_NAME}[/blue]")
        logger.info(f"[cyan]⛽ TEA balance (untuk gas): {eth_balance:.6f} TEA[/cyan]")
        logger.info(f"[green]🔗 Estimasi TX sisa: {estimated_tx_possible} transaksi[/green]")
    except Exception as e:
        logger.error(f"[red]Gagal membaca balance: {e}[/red]")

# Fungsi untuk cek RPC Limit

def check_rpc_limit():
    try:
        latest_block = w3.eth.get_block('latest')  # Cek block terakhir
        logger.info(f"[yellow]🔄 RPC Limit Check: Block terakhir: {latest_block['number']}[/yellow]")
        return False  # Tidak ada limit tercapai, lanjutkan
    except web3.exceptions.Web3Exception as e:
        logger.warning(f"[red]❌ RPC Limit tercapai: {e}[/red]")
        return True  # RPC limit tercapai

# Fungsi kirim token acak dengan adaptive delay

def send_tokens():
    if not wallets_all:
        logger.warning("❗ Daftar wallet kosong.")
        return

    random.shuffle(wallets_all)
    total = len(wallets_all)
    logger.info(f"🚀 Mulai pengiriman batch ke {total} wallet...")

    for i, to_address in enumerate(wallets_all):
        if check_rpc_limit():  # Mengecek RPC limit sebelum pengiriman
            logger.info("[yellow]⏳ Menunggu karena RPC limit tercapai...[/yellow]")
            time.sleep(60)  # Tunggu selama 60 detik sebelum melanjutkan pengiriman

        try:
            amount = random.randint(10, 100) * (10 ** decimals)  # Nominal acak antara 10 dan 100 token
            nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
            tx = token_contract.functions.transfer(to_address, amount).build_transaction({
                'from': SENDER_ADDRESS,
                'gas': 200000,  # Sesuaikan dengan gas limit yang cukup
                'gasPrice': w3.eth.gas_price,
                'nonce': nonce,
            })

            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            logger.info(f"[green]✅ Sent {amount / (10 ** decimals):.4f} {TOKEN_NAME} to {to_address} | Hash: {tx_hash.hex()}[/green]")

            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))  # Delay acak

        except Exception as e:
            logger.error(f"[red]❌ Failed to send to {to_address}: {e}[/red]")

# Fungsi utama bot

def main():
    schedule.every(1).hour.do(send_tokens)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    log_balances()  # Tampilkan saldo
    main()
