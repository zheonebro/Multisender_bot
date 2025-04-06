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
from rich.progress import Progress, track
from rich.table import Table

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
transaction_log_path = os.path.join(log_dir, "transactions.log")

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

# Global Rate Limiting State
rate_limit_lock = threading.Lock()
last_sent_time = 0
current_nonce = None
nonce_lock = threading.Lock()

failed_addresses = []

def initialize_nonce():
    global current_nonce
    current_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")

def load_wallets():
    wallets = []
    sent_set = set()
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r") as f:
            sent_set = set(line.strip().lower() for line in f.readlines())
    with open(CSV_FILE, "r") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            address, amount = row[0].strip(), row[1].strip()
            if address.lower() not in sent_set:
                wallets.append((address, float(amount)))
    return wallets

def log_transaction(to_address, amount, status, tx_hash_or_error):
    with open(transaction_log_path, "a", encoding="utf-8") as f:
        timestamp = datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{timestamp},{to_address},{amount},{status},{tx_hash_or_error}\n")

@tenacity.retry(stop=tenacity.stop_after_attempt(3), wait=tenacity.wait_fixed(2))
def send_token(to_address, amount_float):
    global current_nonce
    try:
        to = web3.Web3.to_checksum_address(to_address)
        token = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
        decimals = token.functions.decimals().call()
        amount = int(amount_float * (10 ** decimals))

        gas_price = w3.eth.gas_price
        if gas_price > web3.Web3.to_wei(MAX_GAS_PRICE_GWEI, "gwei"):
            logger.warning(f"⛽ Gas price terlalu tinggi: {w3.from_wei(gas_price, 'gwei')} Gwei")
            return False

        with nonce_lock:
            nonce = current_nonce

        tx = token.functions.transfer(to, amount).build_transaction({
            "from": SENDER_ADDRESS,
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": 60000,
        })

        signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)

        with nonce_lock:
            current_nonce += 1

        tx_hash_str = tx_hash.hex()
        logger.info(f"✅ TX terkirim ke {to_address} | Jumlah: {amount_float} | TxHash: {tx_hash_str}")
        log_transaction(to_address, amount_float, "SUCCESS", tx_hash_str)

        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address.lower()}\n")

        return True

    except Exception as e:
        logger.error(f"❌ Gagal kirim ke {to_address}: {e}")
        log_transaction(to_address, amount_float, "FAILED", str(e))
        failed_addresses.append(to_address)
        return False

# Rate-limited send wrapper
def rate_limited_send(to_address, amount_float):
    global last_sent_time
    with rate_limit_lock:
        now = time.time()
        elapsed = now - last_sent_time
        delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
        if delay > 0:
            time.sleep(delay)
        last_sent_time = time.time()
    return send_token(to_address, amount_float)

# ERC20 ABI
ERC20_ABI = [ ... ]  # Tetap sama seperti sebelumnya

# CLI Dashboard

def show_dashboard():
    wallets = load_wallets()
    total_wallets = len(wallets)
    total_sent = len(open(SENT_FILE).readlines()) if os.path.exists(SENT_FILE) else 0
    table = Table(title="📊 DASHBOARD PENGIRIMAN TOKEN")
    table.add_column("Keterangan", style="cyan")
    table.add_column("Jumlah", style="magenta")

    table.add_row("Total Wallet di CSV", str(total_wallets + total_sent))
    table.add_row("Sudah Dikirim", str(total_sent))
    table.add_row("Menunggu Dikirim", str(total_wallets))

    console.print(table)

def process_batch():
    wallets = load_wallets()
    if not wallets:
        logger.info("✅ Tidak ada wallet yang perlu dikirimi saat ini.")
        return

    logger.info(f"🔄 Memulai pengiriman ke {len(wallets)} wallet...")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(rate_limited_send, addr, amt): (addr, amt) for addr, amt in wallets}
        for future in as_completed(futures):
            addr, amt = futures[future]
            try:
                result = future.result()
            except Exception as e:
                logger.error(f"❌ Error saat mengirim ke {addr}: {e}")

    if failed_addresses:
        with open("failed_wallets.txt", "w") as f:
            for addr in failed_addresses:
                f.write(f"{addr}\n")
        logger.info(f"⚠️ {len(failed_addresses)} wallet gagal dikirimi. Disimpan di failed_wallets.txt")

def countdown(seconds):
    for i in range(seconds, 0, -1):
        sys.stdout.write(f"\r⏳ Menunggu {i} detik...")
        sys.stdout.flush()
        time.sleep(1)
    print()

def check_logs():
    logger.info("📜 Log terakhir dipantau.")

# Entry point

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-once", action="store_true", help="Jalankan hanya sekali tanpa jadwal")
    parser.add_argument("--dashboard", action="store_true", help="Tampilkan dashboard CLI")
    args = parser.parse_args()

    initialize_nonce()

    if args.dashboard:
        show_dashboard()
        sys.exit()

    if args.run_once:
        start_time = time.time()
        process_batch()
        check_logs()
        logger.info(f"⏱️ Durasi eksekusi: {time.time() - start_time:.2f} detik")
    else:
        logger.info("🕒 Menjalankan dengan penjadwalan setiap 5 menit")
        schedule.every(5).minutes.do(process_batch)
        while True:
            schedule.run_pending()
            countdown(300)

if __name__ == "__main__":
    main()
