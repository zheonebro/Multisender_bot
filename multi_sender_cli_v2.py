import csv
import os
import random
import time
from datetime import datetime, time as dt_time
import threading
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich import box
import web3
import tenacity
import schedule
from tqdm import tqdm
import pytz
import signal

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

MAX_ADDRESS_PER_DAY = 150
DAILY_RANDOM_LIMIT = 200

JAKARTA_TZ = pytz.timezone("Asia/Jakarta")

user_defined_daily_wallet_limit = DAILY_RANDOM_LIMIT
is_running = False

# Time checker
def is_reset_time():
    now = datetime.now(JAKARTA_TZ)
    return now.time() >= dt_time(0, 0) and now.time() < dt_time(0, 1)

def schedule_reset_daily():
    def check_and_reset():
        if is_reset_time():
            reset_sent_wallets()
    schedule.every().minute.do(check_and_reset)

# Load wallet
def load_sent_wallets():
    if not os.path.exists(SENT_FILE):
        return set()
    with open(SENT_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_wallet(address):
    with open(SENT_FILE, 'a') as f:
        f.write(f"{address}\n")

def load_wallets(csv_file, limit):
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

    if len(valid_addresses) > limit:
        valid_addresses = random.sample(valid_addresses, limit)

    return valid_addresses

# Main runner
def run_sending():
    global user_defined_daily_wallet_limit, is_running

    if is_running:
        logger.warning("⚠️ Proses pengiriman masih berlangsung. Menunggu batch selesai...")
        return

    user_input = input_with_timeout("Berapa jumlah maksimal wallet yang ingin dikirimi hari ini?", timeout=10)
    try:
        user_defined_daily_wallet_limit = int(user_input.strip()) if user_input.strip() else DAILY_RANDOM_LIMIT
    except:
        user_defined_daily_wallet_limit = DAILY_RANDOM_LIMIT

    is_running = True
    try:
        logger.info("💡 Starting sender bot...")
        log_balances()
        wallets = load_wallets(CSV_FILE, user_defined_daily_wallet_limit)
        total = len(wallets)
        if total == 0:
            logger.info("🚫 Tidak ada wallet yang akan dikirimi.")
            return

        batches = [wallets[i:i+10] for i in range(0, total, 10)]

        with ThreadPoolExecutor(max_workers=5) as executor:
            for idx, batch in enumerate(batches):
                executor.submit(process_batch, batch, idx+1)
                logger.info(f"🛌 Menunggu selama 300 detik sebelum batch berikutnya...")
                for remaining in range(300, 0, -1):
                    logger.info(f"⏸ Idle... {remaining} detik tersisa")
                    time.sleep(1)
    except KeyboardInterrupt:
        logger.warning("⛔ Pengguna menghentikan proses. Menutup dengan aman...")
    finally:
        is_running = False

# Interaktif menu
def input_with_timeout(prompt, timeout=10):
    console.print(f"{prompt} [default: 200, auto dalam {timeout} detik]: ", end="", style="bold yellow")
    def timeout_handler(signum, frame):
        raise TimeoutError
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout)
    try:
        return input()
    except TimeoutError:
        print("\n⏱️ Tidak ada input. Gunakan default 200.")
        return ""
    finally:
        signal.alarm(0)

if __name__ == "__main__":
    run_sending()


# interactive_menu tidak berubah dari versi sebelumnya
# CLI tidak berubah dari versi sebelumnya
