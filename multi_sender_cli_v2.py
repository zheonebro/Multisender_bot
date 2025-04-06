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
import sys
from rich.progress import Progress

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

JAKARTA_TZ = pytz.timezone("Asia/Jakarta")

class JakartaFormatter(logging.Formatter):
    converter = lambda *args: datetime.now(JAKARTA_TZ).timetuple()
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, JAKARTA_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        else:
            return dt.isoformat()

logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

stream_handler = logging.StreamHandler()
file_handler = logging.FileHandler(log_path, encoding="utf-8")

formatter = JakartaFormatter(fmt="%(asctime)s %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]")
stream_handler.setFormatter(formatter)
file_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)

logger.info("🕒 Logging timezone aktif: Asia/Jakarta")

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
        "outputs": [
            {"internalType": "bool", "name": "", "type": "bool"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [
            {"internalType": "uint8", "name": "", "type": "uint8"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "_owner", "type": "address"}
        ],
        "name": "balanceOf",
        "outputs": [
            {"internalType": "uint256", "name": "balance", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "name",
        "outputs": [
            {"internalType": "string", "name": "", "type": "string"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# Buat instance kontrak
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
TOKEN_DECIMALS = token_contract.functions.decimals().call()

# Load dompet yang sudah dikirim
sent_wallets = set()
if os.path.exists(SENT_FILE):
    with open(SENT_FILE, "r") as f:
        sent_wallets.update(line.strip() for line in f if line.strip())

# Fungsi bantu: baca CSV
def load_wallets():
    with open(CSV_FILE, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        wallets = []
        for row in reader:
            if row["address"] not in sent_wallets:
                try:
                    amount = float(row["amount"])
                except (KeyError, ValueError):
                    amount = round(random.uniform(5, 10), 4)  # default antara 5 dan 10
                wallets.append({"address": row["address"], "amount": amount})
        return wallets

# Cek saldo cukup
def has_sufficient_balance(amount_int):
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
    return balance >= amount_int

# Log saldo
def log_balance():
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
    balance_token = balance / (10 ** TOKEN_DECIMALS)
    logger.info(f"💰 Sisa saldo token: {balance_token:.4f}")

# Inisialisasi nonce
manual_nonce = None
nonce_lock = threading.Lock()

@tenacity.retry(
    wait=tenacity.wait_fixed(5),
    stop=tenacity.stop_after_attempt(3),
    retry=tenacity.retry_if_exception_type(Exception)
)
def safe_get_transaction_count(address, tag):
    return w3.eth.get_transaction_count(address, tag)

def initialize_nonce():
    global manual_nonce
    try:
        manual_nonce = safe_get_transaction_count(SENDER_ADDRESS, 'pending')
        logger.info(f"🚀 Initial nonce dari 'pending': {manual_nonce}")
    except Exception as e:
        logger.warning(f"⚠️ Gagal dapat nonce 'pending', fallback ke 'latest'. Error: {e}")
        manual_nonce = safe_get_transaction_count(SENDER_ADDRESS, 'latest')
        logger.info(f"🪄 Initial nonce dari 'latest': {manual_nonce}")

def get_nonce():
    global manual_nonce
    with nonce_lock:
        if manual_nonce is None:
            manual_nonce = safe_get_transaction_count(SENDER_ADDRESS, 'pending')
        nonce = manual_nonce
        latest_nonce = safe_get_transaction_count(SENDER_ADDRESS, 'pending')
        if nonce < latest_nonce:
            logger.warning(f"🔄 Menyesuaikan nonce dari {nonce} ke {latest_nonce}")
            nonce = latest_nonce
        manual_nonce = nonce + 1
        return nonce

# Kirim token dengan retry otomatis
@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=5, max=60),
    stop=tenacity.stop_after_attempt(5),
    retry=tenacity.retry_if_exception_type(Exception),
    reraise=True
)
def send_token(to_address, amount_float):
    global manual_nonce
    to_address = web3.Web3.to_checksum_address(to_address)
    amount = int(amount_float * (10 ** TOKEN_DECIMALS))

    if not has_sufficient_balance(amount):
        logger.warning(f"🚫 Saldo tidak cukup untuk mengirim {amount_float} ke {to_address}")
        return

    try:
        with nonce_lock:
            nonce = get_nonce()
            tx = token_contract.functions.transfer(to_address, amount).build_transaction({
                'from': SENDER_ADDRESS,
                'nonce': nonce,
                'gas': 100_000,
                'gasPrice': w3.to_wei(MAX_GAS_PRICE_GWEI, 'gwei')
            })
            signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)

        logger.info(f"✅ Transfer ke {to_address} berhasil! TX: {EXPLORER_URL}tx/{tx_hash.hex()}")
        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address}\n")

    except Exception as e:
        logger.error(f"⚠️ Gagal kirim ke {to_address}: {e}")
        with nonce_lock:
            if manual_nonce is not None and manual_nonce > 0:
                manual_nonce -= 1
        raise

# Proses batch
def process_batch():
    total_sent = 0
    wallets = load_wallets()
    logger.info(f"🔎 Ditemukan {len(wallets)} wallet baru untuk diproses")

    log_balance()

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        for row in wallets:
            to_address = row["address"]
            amount = float(row["amount"])

            if DAILY_LIMIT and total_sent + amount > DAILY_LIMIT:
                logger.warning("⛔ Melebihi batas harian. Pengiriman dihentikan sementara.")
                break

            futures.append(executor.submit(send_token, to_address, amount))
            total_sent += amount
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

        for future in tqdm(as_completed(futures), total=len(futures), desc="⏳ Menyelesaikan transaksi"):
            pass

    log_balance()
    logger.info("💤 Menunggu 30 detik sebelum melanjutkan batch berikutnya...")
    time.sleep(30)

# Reset harian
def reset_daily_log():
    with open(SENT_FILE, "w") as f:
        f.write("")
    logger.info("🔄 Daftar wallet terkirim telah di-reset.")

# Cek isi log
def check_logs():
    logger.info("📄 Mengecek isi log terbaru:")
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-10:]
            for line in lines:
                logger.info(line.strip())
    except Exception as e:
        logger.error(f"⚠️ Gagal membaca log: {e}")

# Jadwal
schedule.every().day.at("00:00").do(reset_daily_log)
schedule.every(5).minutes.do(process_batch)
schedule.every(5).minutes.do(check_logs)

def countdown(seconds):
    with Progress(transient=True) as progress:
        task = progress.add_task("[yellow]⏳ Menunggu jadwal berikutnya...", total=seconds)
        for _ in range(seconds):
            time.sleep(1)
            progress.update(task, advance=1)

# Entry point
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true", help="Jalankan hanya sekali tanpa jadwal")
    args = parser.parse_args()

    initialize_nonce()

    if args.run_once:
        process_batch()
        check_logs()
    else:
        logger.info("🕒 Menjalankan dengan penjadwalan setiap 5 menit")
        while True:
            schedule.run_pending()
            countdown(300)

if __name__ == "__main__":
    main()
