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

console = Console()
load_dotenv()

BANNER = """
███████╗██████╗  ██████╗  ██████╗ ██████╗  ██████╗ ███╗   ██╗
██╔════╝██╔══██╗██╔═══██╗██╔════╝ ██╔══██╗██╔═══██╗████╗  ██║
█████╗  ██████╔╝██║   ██║██║  ███╗██████╔╝██║   ██║██╔██╗ ██║
██╔══╝  ██╔══██╗██║   ██║██║   ██║██╔═══╝ ██║   ██║██║╚██╗██║
███████╗██║  ██║╚██████╔╝╚██████╔╝██║     ╚██████╔╝██║ ╚████║
╚══════╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚═╝  ╚═══╝
"""
console.print(Panel.fit(BANNER, title="[bold green]🚀 TEA SEPOLIA TESNET Sender Bot[/bold green]", border_style="cyan", box=box.DOUBLE))

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

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RAW_SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_RAW = os.getenv("TOKEN_CONTRACT")

if not PRIVATE_KEY:
    logger.error("❌ Environment variable PRIVATE_KEY tidak ditemukan di .env!")
if not RAW_SENDER_ADDRESS:
    logger.error("❌ Environment variable SENDER_ADDRESS tidak ditemukan di .env!")
if not RPC_URL:
    logger.error("❌ Environment variable INFURA_URL tidak ditemukan di .env!")
if not PRIVATE_KEY or not RAW_SENDER_ADDRESS or not RPC_URL:
    exit()

try:
    MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "50"))
except ValueError:
    logger.error("❌ MAX_GAS_PRICE_GWEI harus berupa angka.")
    exit()

SENDER_ADDRESS = web3.Web3.to_checksum_address(RAW_SENDER_ADDRESS)

DAILY_LIMIT_RAW = os.getenv("DAILY_LIMIT", "0")
try:
    DAILY_LIMIT = float(DAILY_LIMIT_RAW)
except ValueError:
    logger.error(f"❌ DAILY_LIMIT salah format: {DAILY_LIMIT_RAW}")
    DAILY_LIMIT = 0

try:
    MIN_DELAY_SECONDS = float(os.getenv("MIN_DELAY_SECONDS", "0.5"))
    MAX_DELAY_SECONDS = float(os.getenv("MAX_DELAY_SECONDS", "2"))
except ValueError:
    logger.error("❌ MIN_DELAY_SECONDS dan MAX_DELAY_SECONDS harus berupa angka.")
    exit()

if not TOKEN_CONTRACT_RAW:
    logger.error("❌ Environment variable 'TOKEN_CONTRACT' tidak ditemukan atau kosong!")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)

CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"

w3 = web3.Web3(web3.Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan! Cek RPC URL")
    exit()

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

token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
decimals = token_contract.functions.decimals().call()
TOKEN_NAME = token_contract.functions.name().call()

def log_balances():
    try:
        token_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** decimals)
        native_balance = w3.eth.get_balance(SENDER_ADDRESS) / (10 ** 18)

        logger.info(f"📦 Token {TOKEN_NAME} balance: {token_balance:.4f}")
        logger.info(f"⛽ Native token balance (ETH): {native_balance:.4f}")
    except Exception as e:
        logger.error(f"❌ Gagal membaca saldo: {e}")

nonce_lock = threading.Lock()
global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
wallets_all = []

MAX_ADDRESS_PER_DAY = 150
DAILY_RANDOM_LIMIT = 200

JAKARTA_TZ = pytz.timezone("Asia/Jakarta")

user_defined_daily_wallet_limit = DAILY_RANDOM_LIMIT
is_running = False
has_reset_today = False

def is_reset_time():
    now = datetime.now(JAKARTA_TZ)
    return now.time() >= dt_time(0, 0) and now.time() < dt_time(0, 1)

def reset_sent_wallets():
    if os.path.exists(SENT_FILE):
        os.remove(SENT_FILE)
        logger.info("🔄 Daftar wallet terkirim telah direset untuk hari baru.")

def schedule_reset_daily():
    def check_and_reset():
        global has_reset_today
        if is_reset_time() and not has_reset_today:
            reset_sent_wallets()
            has_reset_today = True
        elif not is_reset_time():
            has_reset_today = False
    schedule.every().minute.do(check_and_reset)

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

def process_batch(batch, batch_number):
    logger.info(f"🛠️ Memproses batch ke-{batch_number} dengan {len(batch)} wallet...")
    for wallet in batch:
        time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
        logger.info(f"✅ Token dikirim ke: {wallet}")
        save_sent_wallet(wallet)

def input_with_timeout(prompt, timeout=10):
    result = [None]

    def get_input():
        try:
            result[0] = input(f"{prompt} [default: 200, auto dalam {timeout} detik]: ")
        except Exception:
            result[0] = ""

    input_thread = threading.Thread(target=get_input)
    input_thread.start()
    input_thread.join(timeout)

    if input_thread.is_alive():
        print("\n⏱️ Tidak ada input. Gunakan default 200.")
        return ""
    return result[0]

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

        for idx, batch in enumerate(batches):
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(process_batch, batch, idx+1)]
                for future in as_completed(futures):
                    future.result()
            logger.info(f"📈 Batch {idx+1} selesai. Menunggu 300 detik...")
            for remaining in range(300, 0, -1):
                logger.info(f"⏸ Idle... {remaining} detik tersisa")
                time.sleep(1)
    except KeyboardInterrupt:
        logger.warning("⛔ Pengguna menghentikan proses. Menutup dengan aman...")
    finally:
        is_running = False

if __name__ == "__main__":
    schedule_reset_daily()
    while True:
        schedule.run_pending()
        run_sending()
        time.sleep(5)
