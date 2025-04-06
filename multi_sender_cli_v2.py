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

logger.info("\ud83d\udd52 Logging timezone aktif: Asia/Jakarta")


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
daily_address_sent = 0

JAKARTA_TZ = pytz.timezone("Asia/Jakarta")

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
    global daily_address_sent
    with open(SENT_FILE, 'a') as f:
        f.write(f"{address}\n")
    daily_address_sent += 1

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

# 🔁 Reset otomatis sent_wallets.txt setiap hari

def reset_sent_wallets():
    global daily_sent_total, daily_address_sent
    try:
        open(SENT_FILE, 'w').close()
        daily_sent_total = 0
        daily_address_sent = 0
        logger.info("🗑️ File sent_wallets.txt telah direset (dikosongkan).")
    except Exception as e:
        logger.error(f"❌ Gagal mereset sent_wallets.txt: {e}")

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
    global global_nonce, daily_sent_total, daily_address_sent
    for i, to_address in enumerate(tqdm(addresses_batch, desc=f"BATCH {batch_id}")):
        try:
            token_amount = random.randint(MIN_TOKEN, MAX_TOKEN)
            if DAILY_LIMIT > 0 and (daily_sent_total + token_amount) > DAILY_LIMIT:
                logger.warning(f"[BATCH {batch_id}] ⚠️ Batas token harian tercapai, menghentikan pengiriman.")
                return

            if daily_address_sent >= MAX_ADDRESS_PER_DAY:
                logger.warning(f"[BATCH {batch_id}] ⚠️ Jumlah address harian ({MAX_ADDRESS_PER_DAY}) tercapai. Menghentikan pengiriman.")
                return

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

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                logger.info(f"[BATCH {batch_id}] ✅ Sent {token_amount} {TOKEN_NAME} to {to_address}")
                logger.info(f"[TX SUCCESS] {EXPLORER_URL}tx/{tx_hash.hex()}")
                save_sent_wallet(to_address)
                daily_sent_total += token_amount
            else:
                logger.error(f"[BATCH {batch_id}] ❌ TX FAILED {EXPLORER_URL}tx/{tx_hash.hex()}")
        except Exception as e:
            logger.error(f"[BATCH {batch_id}] ❌ Failed to send to {to_address}: {e}")

        time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

# Main runner

def run_sending():
    logger.info("💡 Starting sender bot...")
    log_balances()
    wallets = load_wallets(CSV_FILE)
    total = len(wallets)
    if total == 0:
        logger.info("🚫 Tidak ada wallet yang akan dikirimi.")
        return

    batches = [wallets[i:i+BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]

    try:
        with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as executor:
            for idx, batch in enumerate(batches):
                executor.submit(process_batch, batch, idx+1)
                logger.info(f"🛌 Menunggu selama {IDLE_AFTER_BATCH_SECONDS} detik sebelum batch berikutnya...")
                for remaining in range(IDLE_AFTER_BATCH_SECONDS, 0, -1):
                    logger.info(f"⏸ Idle... {remaining} detik tersisa")
                    time.sleep(1)
    except KeyboardInterrupt:
        logger.warning("⛔ Pengguna menghentikan proses. Menutup dengan aman...")

# Interaktif menu

def interactive_menu():
    while True:
        console.print("\n[bold cyan]📋 MENU UTAMA[/bold cyan]")
        console.print("1. Kirim token sekarang (otomatis)")
        console.print("2. Lihat saldo")
        console.print("3. Reset daftar yang sudah dikirim")
        console.print("4. Keluar")

        pilihan = input("Pilih opsi (1-4): ").strip()

        if pilihan == "1":
            logger.info("[AUTO MODE - MANUAL] Bot akan terus mengirim batch secara otomatis.")
            schedule.every(IDLE_AFTER_BATCH_SECONDS).seconds.do(run_sending)
            schedule_reset_daily()
            run_sending()
            while True:
                schedule.run_pending()
                time.sleep(1)
        elif pilihan == "2":
            log_balances()
        elif pilihan == "3":
            reset_sent_wallets()
        elif pilihan == "4":
            print("👋 Keluar...")
            break
        else:
            print("❌ Pilihan tidak valid!")

# CLI

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Mode otomatis berjalan terus dengan delay antar batch")
    args = parser.parse_args()

    if args.auto:
        logger.info("[AUTO MODE] Bot akan terus mengirim batch secara otomatis.")
        schedule.every(IDLE_AFTER_BATCH_SECONDS).seconds.do(run_sending)
        schedule_reset_daily()
        run_sending()
        while True:
            schedule.run_pending()
            time.sleep(1)
    else:
        interactive_menu()
