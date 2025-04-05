import csv
import os
import random
import time
from datetime import datetime
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich import box
import web3
import tenacity
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
console.print(Panel.fit(BANNER, title="[bold green]🚀 TEA SEPOLIA TESNET Sender Bot[/bold green]", border_style="cyan", box=box.DOUBLE))

# Setup logging
log_dir = "runtime_logs"
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "runtime.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_path, encoding="utf-8")
    ]
)
logger = logging.getLogger("bot")

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RAW_SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_RAW = os.getenv("TOKEN_CONTRACT")
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "50"))
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

CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"

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
global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
wallets_all = []

# Load wallet

def load_sent_wallets():
    if not os.path.exists(SENT_FILE):
        return set()
    with open(SENT_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_wallet(address):
    with open(SENT_FILE, 'a') as f:
        f.write(f"{address}\n")

def load_wallets(csv_file):
    sent_addresses = load_sent_wallets()
    valid_addresses = []
    if not os.path.exists(csv_file):
        logger.error(f"❌ File {csv_file} tidak ditemukan!")
        return valid_addresses

    with open(csv_file, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                address = web3.Web3.to_checksum_address(row['address'].strip())
                if address not in sent_addresses:
                    valid_addresses.append(address)
            except:
                continue
    return valid_addresses

wallets_all = load_wallets(CSV_FILE)

# Monitoring balance
def log_balances():
    try:
        token_balance_raw = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
        token_balance = token_balance_raw / (10 ** decimals)

        eth_balance_wei = w3.eth.get_balance(SENDER_ADDRESS)
        eth_balance = w3.from_wei(eth_balance_wei, 'ether')

        gas_price = w3.eth.gas_price
        estimated_gas_per_tx = 50000
        estimated_tx_possible = int(eth_balance_wei / (estimated_gas_per_tx * gas_price))

        logger.info(f"📊 Token balance: {token_balance:.4f} {TOKEN_NAME}")
        logger.info(f"⛽ TEA balance (gas): {eth_balance:.6f} TEA")
        logger.info(f"🔗 Estimasi TX sisa: {estimated_tx_possible} transaksi")
    except Exception as e:
        logger.error(f"Gagal membaca balance: {e}")

# Retry decorator
@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=5, max=60),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(Exception)
)
def safe_send_transaction(tx):
    try:
        signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        return w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as e:
        logger.error(f"Gagal kirim TX: {e}")
        raise

# Multi-thread
THREAD_WORKERS = 5
BATCH_SIZE = 10
IDLE_AFTER_BATCH_SECONDS = 300

MIN_TOKEN = 5
MAX_TOKEN = 50

def process_batch(addresses_batch, batch_id):
    global global_nonce
    for i, to_address in enumerate(addresses_batch):
        try:
            token_amount = random.randint(MIN_TOKEN, MAX_TOKEN)
            amount = token_amount * (10 ** decimals)
            with nonce_lock:
                nonce = global_nonce
                global_nonce += 1
                tx = token_contract.functions.transfer(to_address, amount).build_transaction({
                    'from': SENDER_ADDRESS,
                    'nonce': nonce,
                    'gas': 60000,
                    'gasPrice': min(w3.eth.gas_price, w3.to_wei(MAX_GAS_PRICE_GWEI, 'gwei'))
                })
                tx_hash = safe_send_transaction(tx)

            logger.info(f"[BATCH {batch_id}] ✅ Sent {token_amount} {TOKEN_NAME} to {to_address}")
            logger.info(f"[TX] https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
            save_sent_wallet(to_address)
        except Exception as e:
            logger.error(f"[BATCH {batch_id}] ❌ Failed to send to {to_address}: {e}")

        time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
