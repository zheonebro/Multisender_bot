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
wallets_all = []

# Load wallet
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
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    return w3.eth.send_raw_transaction(signed.raw_transaction)

# Multi-thread
THREAD_WORKERS = 5
BATCH_SIZE = 10
IDLE_AFTER_BATCH_SECONDS = 300

MIN_TOKEN = 5
MAX_TOKEN = 50

def process_batch(addresses_batch, batch_id):
    for i, to_address in enumerate(addresses_batch):
        try:
            token_amount = random.randint(MIN_TOKEN, MAX_TOKEN)
            amount = token_amount * (10 ** decimals)
            with nonce_lock:
                nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
                tx = token_contract.functions.transfer(to_address, amount).build_transaction({
                    'from': SENDER_ADDRESS,
                    'nonce': nonce,
                    'gas': 60000,
                    'gasPrice': w3.eth.gas_price
                })
                tx_hash = safe_send_transaction(tx)

            logger.info(f"[BATCH {batch_id}] ✅ Sent {token_amount} {TOKEN_NAME} to {to_address}")
            logger.info(f"[TX] https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
        except Exception as e:
            logger.error(f"[BATCH {batch_id}] ❌ Failed to send to {to_address}: {e}")

        time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

def send_tokens_multithreaded():
    if not wallets_all:
        logger.warning("❗ Daftar wallet kosong.")
        return

    logger.info("🚀 Memulai pengiriman multi-threaded...")

    random.shuffle(wallets_all)
    batches = [wallets_all[i:i + BATCH_SIZE] for i in range(0, len(wallets_all), BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as executor:
        futures = {executor.submit(process_batch, batch, idx+1): idx+1 for idx, batch in enumerate(batches)}

        for future in as_completed(futures):
            batch_id = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f"❌ BATCH {batch_id} failed with error: {e}")

    logger.info("🎉 Semua batch selesai dikirim.")
    logger.info(f"💤 Idle selama {IDLE_AFTER_BATCH_SECONDS} detik...")
    time.sleep(IDLE_AFTER_BATCH_SECONDS)

# CLI Menu

def cli_menu():
    global MIN_TOKEN, MAX_TOKEN
    while True:
        console.print("\n[bold cyan]Menu:[/bold cyan]\n1. Kirim Token Sekarang\n2. Jadwalkan Kirim Harian\n3. Keluar", style="bold")
        choice = input("Pilih opsi (1/2/3): ").strip()

        if choice == "1" or choice == "2":
            try:
                min_input = input("Masukkan minimum token per transaksi (default 5): ").strip()
                MIN_TOKEN = int(min_input) if min_input else 5
                max_input = input("Masukkan maksimum token per transaksi (default 50): ").strip()
                MAX_TOKEN = int(max_input) if max_input else 50
            except ValueError:
                MIN_TOKEN, MAX_TOKEN = 5, 50
                logger.warning("Input tidak valid. Menggunakan default 5-50 token.")

            if MIN_TOKEN > MAX_TOKEN:
                MIN_TOKEN, MAX_TOKEN = MAX_TOKEN, MIN_TOKEN
                logger.warning("Minimum lebih besar dari maksimum. Nilai ditukar.")

            if choice == "1":
                log_balances()
                send_tokens_multithreaded()
            else:
                schedule.every().day.at("10:00").do(lambda: [log_balances(), send_tokens_multithreaded()])
                console.print("⏰ Pengiriman dijadwalkan setiap hari pukul 10:00.")
                while True:
                    schedule.run_pending()
                    time.sleep(60)

        elif choice == "3":
            logger.info("👋 Keluar dari program.")
            break
        else:
            console.print("❌ Pilihan tidak valid. Coba lagi.", style="red")

if __name__ == "__main__":
    cli_menu()
