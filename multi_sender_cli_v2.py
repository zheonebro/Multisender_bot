import csv
import os
import random
import time
from datetime import datetime, time as dt_time
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
import pytz
import sys
from rich.progress import Progress
from rich.table import Table
from rich.prompt import Prompt

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

MAX_THREADS = int(os.getenv("MAX_THREADS", 5))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 10))
IDLE_SECONDS = int(os.getenv("IDLE_SECONDS", 30))
MIN_TOKEN_AMOUNT = float(os.getenv("MIN_TOKEN_AMOUNT", "0.1"))  # Batas minimal token per wallet
MAX_TOKEN_AMOUNT = float(os.getenv("MAX_TOKEN_AMOUNT", "100.0"))  # Batas maksimal token per wallet

if not PRIVATE_KEY or not RAW_SENDER_ADDRESS or not RPC_URL:
    logger.error("❌ PRIVATE_KEY, SENDER_ADDRESS, atau INFURA_URL tidak ditemukan di .env!")
    exit()

SENDER_ADDRESS = web3.Web3.to_checksum_address(RAW_SENDER_ADDRESS)

DAILY_LIMIT_RAW = os.getenv("DAILY_LIMIT", "0")
try:
    DAILY_LIMIT = float(DAILY_LIMIT_RAW)
except ValueError:
    DAILY_LIMIT = 0

if not TOKEN_CONTRACT_RAW:
    logger.error("❌ Environment variable 'TOKEN_CONTRACT' tidak ditemukan atau kosong!")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)

CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"

# Log konfigurasi saat startup
logger.info(f"⚙️ Konfigurasi: MIN_TOKEN_AMOUNT={MIN_TOKEN_AMOUNT}, MAX_TOKEN_AMOUNT={MAX_TOKEN_AMOUNT}, DAILY_LIMIT={DAILY_LIMIT}, MAX_THREADS={MAX_THREADS}, BATCH_SIZE={BATCH_SIZE}, IDLE_SECONDS={IDLE_SECONDS}")

# Connect Web3
w3 = web3.Web3(web3.Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan! Cek RPC URL")
    exit()

# Token Contract Setup
TOKEN_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]

token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=TOKEN_ABI)
TOKEN_DECIMALS = token_contract.functions.decimals().call()

# Global Rate Limiting State
rate_limit_lock = threading.Lock()
last_sent_time = 0
current_nonce = None
nonce_lock = threading.Lock()

failed_addresses = []

def initialize_nonce():
    global current_nonce
    current_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")

def get_next_nonce():
    global current_nonce
    with nonce_lock:
        nonce = current_nonce
        current_nonce += 1
        return nonce

def check_balance():
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
    balance_in_tokens = balance / (10 ** TOKEN_DECIMALS)
    logger.info(f"💰 Saldo token pengirim: {balance_in_tokens:.4f}")
    return balance_in_tokens

def load_wallets():
    wallets = []
    sent_set = set()
    try:
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r") as f:
                sent_set = set(line.strip().lower() for line in f.readlines())
        with open(CSV_FILE, "r") as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                address, amount = row[0].strip(), row[1].strip()
                if not w3.is_address(address):
                    logger.warning(f"⚠️ Alamat tidak valid: {address}, dilewati.")
                    continue
                try:
                    amount_float = float(amount)
                    if amount_float < MIN_TOKEN_AMOUNT:
                        logger.warning(f"⚠️ Jumlah {amount_float} untuk {address} di bawah minimum ({MIN_TOKEN_AMOUNT}), dilewati.")
                        continue
                    if amount_float > MAX_TOKEN_AMOUNT:
                        logger.warning(f"⚠️ Jumlah {amount_float} untuk {address} melebihi maksimum ({MAX_TOKEN_AMOUNT}), dilewati.")
                        continue
                    if address.lower() not in sent_set:
                        wallets.append((address, amount_float))
                except ValueError:
                    logger.warning(f"⚠️ Jumlah tidak valid untuk alamat {address}: {amount}, dilewati.")
    except Exception as e:
        logger.error(f"❌ Gagal membaca file wallet: {e}")
    return wallets

def log_transaction(to_address, amount, status, tx_hash_or_error):
    with open(transaction_log_path, "a", encoding="utf-8") as f:
        timestamp = datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{timestamp},{to_address},{amount},{status},{tx_hash_or_error}\n")

def display_transaction_logs():
    if not os.path.exists(transaction_log_path):
        console.print("📬 Belum ada transaksi yang dicatat.", style="yellow")
        return

    table = Table(title="📌 LOG TRANSAKSI TOKEN (SELURUH DATA)", box=box.SIMPLE_HEAVY)
    table.add_column("No", justify="center", style="dim")
    table.add_column("Waktu", style="dim", width=20)
    table.add_column("Alamat Tujuan", style="cyan")
    table.add_column("Jumlah", justify="right", style="green")
    table.add_column("Status", style="bold")
    table.add_column("TxHash/Error", overflow="fold")

    sukses = gagal = 0
    total_token = 0.0

    with open(transaction_log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
        for idx, line in enumerate(lines, 1):
            parts = line.strip().split(",")
            if len(parts) >= 5:
                waktu, alamat, jumlah, status, detail = parts
                try:
                    jumlah_float = float(jumlah)
                    total_token += jumlah_float
                except ValueError:
                    jumlah_float = 0

                if status.upper() == "SUCCESS":
                    sukses += 1
                    explorer_link = f"[link=https://sepolia.tea.xyz/tx/{detail}]🔗 {detail[:10]}...[/link]"
                    table.add_row(str(idx), waktu, alamat, f"{jumlah_float:.4f}", f"[green]{status}[/green]", explorer_link)
                else:
                    gagal += 1
                    table.add_row(str(idx), waktu, alamat, f"{jumlah_float:.4f}", f"[red]{status}[/red]", detail)

    console.print(table)
    console.print(f"✅ Total Sukses: [green]{sukses}[/green] | ❌ Gagal: [red]{gagal}[/red] | 📦 Total Token Dikirim: [cyan]{total_token:.4f}[/cyan]", style="bold")

def check_logs():
    logger.info("📜 Menampilkan seluruh log transaksi...")
    display_transaction_logs()

@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=10),
    retry=tenacity.retry_if_exception_type(Exception),
    reraise=True
)
def _send_token_with_retry(to_address, amount):
    from_address = SENDER_ADDRESS
    to_address = web3.Web3.to_checksum_address(to_address)
    scaled_amount = int(amount * (10 ** TOKEN_DECIMALS))

    # Estimasi gas secara dinamis
    gas_estimate = token_contract.functions.transfer(to_address, scaled_amount).estimate_gas({'from': from_address})
    gas_limit = int(gas_estimate * 1.2)  # Tambah 20% buffer

    tx = token_contract.functions.transfer(to_address, scaled_amount).build_transaction({
        'from': from_address,
        'nonce': get_next_nonce(),
        'gas': gas_limit,
        'gasPrice': w3.to_wei(min(MAX_GAS_PRICE_GWEI, w3.eth.gas_price / 10**9), 'gwei')
    })

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if tx_receipt.status != 1:
        raise Exception("Status receipt gagal")

    return tx_hash.hex()

def send_token_threadsafe(to_address, amount):
    try:
        tx_hash = _send_token_with_retry(to_address, amount)
        logger.info(f"✅ Token terkirim ke {to_address} | Amount: {amount} | TxHash: {tx_hash}")
        log_transaction(to_address, amount, "SUCCESS", tx_hash)
        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address}\n")
    except Exception as e:
        logger.error(f"❌ Gagal mengirim ke {to_address} setelah retry: {e}")
        log_transaction(to_address, amount, "FAILED", str(e))
        failed_addresses.append((to_address, amount))
    delay = random.uniform(0.5, 2.0)
    logger.info(f"⏱️ Delay adaptif {delay:.2f} detik sebelum lanjut...")
    time.sleep(delay)

def reset_sent_wallets():
    try:
        with open(SENT_FILE, "w") as f:
            f.write("")  # Kosongkan file
        logger.info("🔄 File sent_wallets.txt telah direset.")
    except Exception as e:
        logger.error(f"❌ Gagal mereset sent_wallets.txt: {e}")

def send_token_batch(wallets):
    total_sent = 0.0
    for i in range(0, len(wallets), BATCH_SIZE):
        logger.info("🔄 Menginisialisasi ulang nonce sebelum batch baru...")
        initialize_nonce()
        batch = wallets[i:i + BATCH_SIZE]
        logger.info(f"🚀 Memproses batch {i // BATCH_SIZE + 1} ({len(batch)} wallet)...")
        with Progress() as progress:
            task = progress.add_task("Mengirim token...", total=len(batch))
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = [executor.submit(send_token_threadsafe, addr, amt) for addr, amt in batch]
                for _ in as_completed(futures):
                    progress.advance(task)
        logger.info(f"📈 Menunggu {IDLE_SECONDS} detik sebelum batch berikutnya...")
        time.sleep(IDLE_SECONDS)

        if DAILY_LIMIT > 0:
            total_sent += sum([amt for _, amt in batch])
            if total_sent >= DAILY_LIMIT:
                logger.warning(f"🚘 Mencapai batas harian {DAILY_LIMIT}, berhenti sementara.")
                console.print(f"[bold red]ℹ️ Limit harian tercapai: {total_sent:.4f}/{DAILY_LIMIT} token telah dikirim hari ini.[/bold red]")
                console.print("[bold yellow]Pilih opsi:[/bold yellow]")
                console.print("[1] Reset manual limit harian (kosongkan sent_wallets.txt)")
                console.print("[2] Tunggu jadwal reset otomatis besok pukul 08:00 WIB")
                pilihan = Prompt.ask("Pilih opsi", choices=["1", "2"], default="2")
                if pilihan == "1":
                    reset_sent_wallets()
                    logger.info("🔄 Memulai ulang pengiriman setelah reset manual...")
                    return send_token_batch(wallets)  # Rekursif untuk melanjutkan
                else:
                    logger.info("⏳ Menunggu jadwal reset otomatis besok pukul 08:00 WIB.")
                    return False  # Hentikan proses
    return True  # Proses selesai tanpa mencapai limit

def retry_failed_addresses():
    global failed_addresses
    if not failed_addresses:
        logger.info("✅ Tidak ada alamat yang gagal untuk dicoba ulang.")
        return
    logger.info(f"🔄 Mencoba ulang {len(failed_addresses)} alamat yang gagal...")
    send_token_batch(failed_addresses)
    failed_addresses = []

def main():
    logger.info("🟢 Fungsi `main()` dijalankan dari scheduler atau manual.")
    wallets = load_wallets()
    if not wallets:
        logger.info("🚫 Tidak ada wallet baru untuk dikirim.")
        console.print("[bold yellow]ℹ️ Tidak ada wallet baru untuk dikirim atau semua telah mencapai limit harian.[/bold yellow]")
        return
    required_amount = sum([amt for _, amt in wallets])
    balance = check_balance()
    if balance < required_amount:
        logger.error(f"❌ Saldo tidak cukup! Dibutuhkan: {required_amount:.4f}, Tersedia: {balance:.4f}")
        return
    logger.info(f"💰 Jumlah wallet yang akan diproses: {len(wallets)}")
    send_token_batch(wallets)

def run_cli():
    while True:
        console.print("\n[bold cyan]=== MENU UTAMA ===[/bold cyan]", style="cyan")
        console.print(f"[bold yellow]Batas Token per Wallet: {MIN_TOKEN_AMOUNT} - {MAX_TOKEN_AMOUNT}[/bold yellow]")
        console.print("[1] Jalankan pengiriman token sekarang")
        console.print("[2] Tampilkan log transaksi")
        console.print("[3] Jalankan mode penjadwalan (scheduler)")
        console.print("[4] Coba ulang alamat yang gagal")
        console.print("[0] Keluar")

        pilihan = Prompt.ask("Pilih opsi", choices=["0", "1", "2", "3", "4"], default="0")

        if pilihan == "1":
            main()
        elif pilihan == "2":
            check_logs()
        elif pilihan == "3":
            schedule.every().day.at("08:00").do(main)
            logger.info("🔌 Menjadwalkan pengiriman token setiap hari pukul 08:00 WIB")
            while True:
                schedule.run_pending()
                logger.info("💤 Bot aktif. Menunggu jadwal pengiriman selanjutnya...")
                time.sleep(60)
        elif pilihan == "4":
            retry_failed_addresses()
        elif pilihan == "0":
            console.print("👋 Keluar dari program.", style="bold red")
            break

if __name__ == "__main__":
    run_cli()
