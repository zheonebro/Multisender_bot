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

        logger.info(f"📊 [bold]Token balance:[/bold] {token_balance:.4f} {TOKEN_NAME}")
        logger.info(f"⛽ [bold]TEA balance (untuk gas):[/bold] {eth_balance:.6f} TEA")
        logger.info(f"🔗 [bold]Estimasi TX sisa:[/bold] {estimated_tx_possible} transaksi")
    except Exception as e:
        logger.error(f"[red]Gagal membaca balance: {e}[/red]")

# Fungsi kirim token dengan log lebih menarik dan detail

def send_tokens():
    if not wallets_all:
        logger.warning("❗ Daftar wallet kosong.")
        return

    total_addresses = len(wallets_all)
    logger.info(f"🚀 **Token Transfer Started** 🚀")
    logger.info(f"--------------------------------")
    logger.info(f"🕒 {datetime.now().strftime('%H:%M:%S')} → **Total Alamat**: {total_addresses}")
    logger.info(f"📊 **Token Balance**: {token_balance:.4f} {TOKEN_NAME}")
    logger.info(f"⛽ **TEA Balance**: {eth_balance:.6f} TEA (Enough for {estimated_tx_possible} transactions)")
    logger.info(f"--------------------------------")

    random.shuffle(wallets_all)
    for i, to_address in enumerate(wallets_all):
        try:
            # Menghitung jumlah token yang akan dikirim secara acak
            amount = random.randint(10, 100) * (10 ** decimals)
            nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
            tx = token_contract.functions.transfer(to_address, amount).build_transaction({
                'from': SENDER_ADDRESS,
                'nonce': nonce,
                'gas': 60000,
                'gasPrice': w3.eth.gas_price
            })
            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info(f"🔄 **Sending Tokens to Address {i+1}:**")
            logger.info(f"💰 Amount: {amount / (10 ** decimals)} {TOKEN_NAME}")
            logger.info(f"🔗 Transaction Hash: `{tx_hash.hex()}`")
            logger.info(f"⏳ **Waiting** for transaction to confirm...")
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
            logger.info(f"✅ **Transaction Successful!**")
            logger.info(f"🕒 {datetime.now().strftime('%H:%M:%S')} → **Sent {amount / (10 ** decimals)} {TOKEN_NAME}** to Address {i+1}")
            logger.info(f"🔗 **Transaction Hash**: `{tx_hash.hex()}`")
            logger.info(f"🧑‍💻 **Address**: `{to_address}`")
            logger.info(f"--------------------------------")
        except Exception as e:
            if "too many requests" in str(e).lower():
                logger.warning(f"⚠️ **RPC Limit Hit**:")
                logger.warning(f"🕒 {datetime.now().strftime('%H:%M:%S')} → **Waiting 60 seconds** before resuming...")
                time.sleep(60)
            else:
                logger.error(f"❌ **Failed to send to {to_address}: {e}**")
    logger.info(f"🎉 **Batch Completed!**")
    logger.info(f"🕒 {datetime.now().strftime('%H:%M:%S')} → **Total Sent Tokens**: {total_tokens_sent}")
    logger.info(f"--------------------------------")

# Main function
def main():
    log_balances()
    send_tokens()

if __name__ == "__main__":
    main()
