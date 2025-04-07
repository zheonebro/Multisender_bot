import csv
import os
import random
import time
from datetime import datetime, time as dt_time, timedelta
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
import requests
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.prompt import Prompt, IntPrompt

# Init
console = Console()
load_dotenv()

# Banner dan setup logging
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

file_handler = logging.FileHandler(log_path, encoding="utf-8")
stream_handler = logging.StreamHandler()

formatter = JakartaFormatter(fmt="%(asctime)s %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)

console.print("[bold green]🟢 Bot dimulai. Log detail tersedia di runtime.log[/bold green]")
logger.info("🕒 Logging timezone aktif: Asia/Jakarta")

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RAW_SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_RAW = os.getenv("TOKEN_CONTRACT")
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "100"))
EXPLORER_URL = "https://sepolia.tea.xyz/tx/"

MAX_THREADS = int(os.getenv("MAX_THREADS", 5))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", 10))
IDLE_SECONDS = int(os.getenv("IDLE_SECONDS", 30))
MIN_TOKEN_AMOUNT = 10.0
MAX_TOKEN_AMOUNT = 50.0
DAILY_WALLET_LIMIT = int(os.getenv("DAILY_WALLET_LIMIT", 200))

if not PRIVATE_KEY or not RAW_SENDER_ADDRESS or not RPC_URL:
    logger.error("❌ PRIVATE_KEY, SENDER_ADDRESS, atau INFURA_URL tidak ditemukan di .env!")
    console.print("[bold red]❌ Konfigurasi .env tidak lengkap. Periksa runtime.log untuk detail.[/bold red]")
    exit()

SENDER_ADDRESS = web3.Web3.to_checksum_address(RAW_SENDER_ADDRESS)

if not TOKEN_CONTRACT_RAW:
    logger.error("❌ Environment variable 'TOKEN_CONTRACT' tidak ditemukan atau kosong!")
    console.print("[bold red]❌ Konfigurasi TOKEN_CONTRACT hilang.[/bold red]")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)

CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"

logger.info(f"⚙️ Konfigurasi: MIN_TOKEN_AMOUNT={MIN_TOKEN_AMOUNT}, MAX_TOKEN_AMOUNT={MAX_TOKEN_AMOUNT}, DAILY_WALLET_LIMIT={DAILY_WALLET_LIMIT}, MAX_THREADS={MAX_THREADS}, BATCH_SIZE={BATCH_SIZE}, IDLE_SECONDS={IDLE_SECONDS}, MAX_GAS_PRICE_GWEI={MAX_GAS_PRICE_GWEI}")

# Connect Web3
w3 = web3.Web3(web3.Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan! Cek RPC URL")
    console.print("[bold red]❌ Gagal terhubung ke jaringan. Periksa RPC URL di .env.[/bold red]")
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

# Fungsi Gas Price (tanpa logging kecuali error)
def get_sepolia_tea_gas_price():
    url = "https://sepolia.tea.xyz/api/v1/gas-price-oracle"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        gas_price_gwei = float(data.get("fast", 0)) * 1.2  # Buffer 20%
        return min(gas_price_gwei, MAX_GAS_PRICE_GWEI)
ตรี
    except requests.RequestException as e:
        logger.error(f"❌ Gagal mengambil gas price dari Sepolia TEA: {e}")
        network_gas_price = w3.eth.gas_price / 10**9 * 1.2  # Buffer 20%
        return min(network_gas_price, MAX_GAS_PRICE_GWEI)

# Fungsi Pembatalan Transaksi
def cancel_transaction(tx_hash, nonce):
    cancel_tx = {
        'from': SENDER_ADDRESS,
        'to': SENDER_ADDRESS,
        'value': 0,
        'nonce': nonce,
        'gas': 21000,
        'gasPrice': w3.to_wei(get_sepolia_tea_gas_price() * 1.5, 'gwei')  # Gas lebih tinggi
    }
    signed_cancel_tx = w3.eth.account.sign_transaction(cancel_tx, PRIVATE_KEY)
    cancel_hash = w3.eth.send_raw_transaction(signed_cancel_tx.raw_transaction)
    logger.info(f"🚫 Membatalkan transaksi {tx_hash} dengan {cancel_hash.hex()}")
    return cancel_hash

# Sinkronisasi Nonce (tanpa logging ke console)
def initialize_nonce():
    global current_nonce
    try:
        current_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
    except Exception as e:
        logger.error(f"❌ Gagal menginisialisasi nonce: {e}")
        raise

def get_next_nonce():
    global current_nonce
    with nonce_lock:
        network_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
        if network_nonce > current_nonce:
            current_nonce = network_nonce
        nonce = current_nonce
        current_nonce += 1
        return nonce

def check_balance():
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
    balance_in_tokens = balance / (10 ** TOKEN_DECIMALS)
    logger.info(f"💰 Saldo token pengirim: {balance_in_tokens:.4f}")
    return balance_in_tokens

def load_wallets(ignore_sent=False, limit=None):
    wallets = []
    sent_set = set()
    try:
        if not ignore_sent and os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r") as f:
                sent_set = set(line.strip().lower() for line in f.readlines())
            logger.info(f"📜 Jumlah wallet di sent_wallets.txt: {len(sent_set)}")
        with open(CSV_FILE, "r") as f:
            reader = csv.reader(f)
            raw_wallets = list(reader)
            logger.info(f"📋 Jumlah entri di wallets.csv: {len(raw_wallets)}")
            for row in raw_wallets:
                if not row or len(row) == 0:
                    logger.warning(f"⚠️ Baris kosong: {row}")
                    continue
                address = row[0].strip()
                if not w3.is_address(address):
                    logger.warning(f"⚠️ Alamat tidak valid: {address}, dilewati.")
                    continue
                checksummed_address = w3.to_checksum_address(address)
                if ignore_sent or checksummed_address.lower() not in sent_set:
                    amount = random.uniform(MIN_TOKEN_AMOUNT, MAX_TOKEN_AMOUNT)
                    wallets.append((checksummed_address, amount))
                    logger.debug(f"✅ Menambahkan {checksummed_address} dengan jumlah acak {amount:.4f}")
                else:
                    logger.debug(f"ℹ️ {checksummed_address} sudah ada di sent_wallets.txt, dilewati.")
    except Exception as e:
        logger.error(f"❌ Gagal membaca file wallet: {e}")
    
    if limit is not None and limit < len(wallets):
        wallets = wallets[:limit]
        logger.info(f"📏 Jumlah wallet dibatasi menjadi: {limit}")
    
    logger.info(f"✅ Jumlah wallet valid yang dimuat: {len(wallets)}")
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
    table.add_column("Explorer Link", overflow="fold")

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
                    explorer_link = f"[link={EXPLORER_URL}{detail}]🔗 {detail[:10]}...[/link]"
                    table.add_row(str(idx), waktu, alamat, f"{jumlah_float:.4f}", f"[green]{status}[/green]", explorer_link)
                else:
                    gagal += 1
                    table.add_row(str(idx), waktu, alamat, f"{jumlah_float:.4f}", f"[red]{status}[/red]", detail)

    console.print(table)
    console.print(f"✅ Total Sukses: [green]{sukses}[/green] | ❌ Gagal: [red]{gagal}[/red] | 📦 Total Token Dikirim: [cyan]{total_token:.4f}[/cyan]", style="bold")

def check_logs():
    logger.info("📜 Menampilkan seluruh log transaksi...")
    display_transaction_logs()

# Fungsi Pengiriman Token (tanpa logging gas ke console)
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

    tea_gas_price = get_sepolia_tea_gas_price()
    gas_price_to_use = min(tea_gas_price, MAX_GAS_PRICE_GWEI)

    try:
        gas_estimate = token_contract.functions.transfer(to_address, scaled_amount).estimate_gas({'from': from_address})
        gas_limit = int(gas_estimate * 1.2)
    except Exception as e:
        logger.error(f"❌ Gagal mengestimasi gas untuk {to_address}: {e}")
        raise Exception(f"Gagal estimasi gas: {e}")

    tx = token_contract.functions.transfer(to_address, scaled_amount).build_transaction({
        'from': from_address,
        'nonce': get_next_nonce(),
        'gas': gas_limit,
        'gasPrice': w3.to_wei(gas_price_to_use, 'gwei')
    })

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    logger.info(f"📤 Transaksi dikirim: {tx_hash.hex()}")

    try:
        tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=600)
        if tx_receipt.status != 1:
            logger.error(f"❌ Transaksi {tx_hash.hex()} gagal di chain: Status {tx_receipt.status}")
            raise Exception(f"Transaksi gagal: Status {tx_receipt.status}")
        logger.info(f"✅ Transaksi {tx_hash.hex()} dikonfirmasi")
        return tx_hash.hex()
    except web3.exceptions.TimeExhausted:
        logger.error(f"⏰ Transaksi {tx_hash.hex()} tidak dikonfirmasi setelah 600 detik")
        cancel_hash = cancel_transaction(tx_hash.hex(), tx['nonce'])
        raise Exception(f"Timeout: Dibatalkan dengan {cancel_hash.hex()}")

def send_token_threadsafe(to_address, amount):
    try:
        tx_hash = _send_token_with_retry(to_address, amount)
        logger.info(f"✅ Token terkirim ke {to_address} | Amount: {amount:.4f} | TxHash: {tx_hash}")
        log_transaction(to_address, amount, "SUCCESS", tx_hash)
        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address}\n")
        return True, amount
    except Exception as e:
        logger.error(f"❌ Gagal mengirim ke {to_address} setelah retry: {e}")
        log_transaction(to_address, amount, "FAILED", str(e))
        failed_addresses.append((to_address, amount))
        return False, 0
    finally:
        delay = random.uniform(0.5, 2.0)
        time.sleep(delay)

def reset_sent_wallets():
    try:
        with open(SENT_FILE, "w") as f:
            f.write("")
        logger.info("🔄 File sent_wallets.txt telah direset secara otomatis.")
        return True
    except Exception as e:
        logger.error(f"❌ Gagal mereset sent_wallets.txt: {e}")
        return False

# Fungsi Batch Diperbarui untuk Hanya Log Token
def send_token_batch(wallets, randomize=False):
    if randomize:
        random.shuffle(wallets)
        logger.info("🔀 Daftar wallet diacak untuk pengiriman acak.")
    total_wallets_sent = 0

    for i in range(0, len(wallets), BATCH_SIZE):
        initialize_nonce()
        batch = wallets[i:i + BATCH_SIZE]
        logger.info(f"🚀 Memulai batch {i // BATCH_SIZE + 1} dengan {len(batch)} wallet")
        batch_wallets_sent = 0
        batch_total_token = 0.0

        with Progress() as progress:
            task = progress.add_task("Mengirim token...", total=len(batch))
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = [executor.submit(send_token_threadsafe, addr, amt) for addr, amt in batch]
                for future in as_completed(futures):
                    success, amount = future.result()
                    if success:
                        batch_wallets_sent += 1
                        batch_total_token += amount
                    progress.advance(task)

        total_wallets_sent += batch_wallets_sent
        logger.info(f"📊 Batch {i // BATCH_SIZE + 1} selesai: {batch_wallets_sent}/{len(batch)} wallet dikirim, Total token: {batch_total_token:.4f}")

        batch_table = Table(title=f"📋 Rekap Batch {i // BATCH_SIZE + 1}", box=box.SIMPLE)
        batch_table.add_column("Kategori", style="cyan")
        batch_table.add_column("Nilai", justify="right", style="green")
        batch_table.add_row("Total Wallet dalam Batch", str(len(batch)))
        batch_table.add_row("Wallet Berhasil Dikirim", str(batch_wallets_sent))
        batch_table.add_row("Wallet Gagal", str(len(batch) - batch_wallets_sent))
        batch_table.add_row("Total Token Dikirim", f"{batch_total_token:.4f}")
        console.print(batch_table)

        if DAILY_WALLET_LIMIT > 0 and total_wallets_sent >= DAILY_WALLET_LIMIT:
            logger.warning(f"🚘 Mencapai batas harian {DAILY_WALLET_LIMIT} wallet, berhenti sementara.")
            console.print(f"[bold red]ℹ️ Limit harian wallet tercapai: {total_wallets_sent}/{DAILY_WALLET_LIMIT} alamat telah dikirim hari ini.[/bold red]")
            console.print(f"[bold green]📦 Total wallet berhasil dikirim: {total_wallets_sent}[/bold green]")
            return False

        logger.info(f"⏳ Menunggu {IDLE_SECONDS} detik sebelum batch berikutnya...")
        time.sleep(IDLE_SECONDS)
    
    logger.info(f"✅ Pengiriman batch selesai. Total wallet dikirim: {total_wallets_sent}")
    console.print(f"[bold green]📦 Total wallet berhasil dikirim: {total_wallets_sent}[/bold green]")
    return True

def retry_failed_addresses():
    global failed_addresses
    if not failed_addresses:
        logger.info("✅ Tidak ada alamat yang gagal untuk dicoba ulang.")
        console.print("[bold green]✅ Tidak ada alamat yang gagal untuk dicoba ulang.[/bold green]")
        return
    logger.info(f"🔄 Mencoba ulang {len(failed_addresses)} alamat yang gagal...")
    send_token_batch(failed_addresses)

def get_next_schedule_time():
    now = datetime.now(JAKARTA_TZ)
    next_run = datetime.combine(now.date(), dt_time(8, 0), tzinfo=JAKARTA_TZ)
    if now >= next_run:
        next_run += timedelta(days=1)
    return next_run

def show_progress_timer():
    next_run = get_next_schedule_time()
    total_seconds = (next_run - datetime.now(JAKARTA_TZ)).total_seconds()
    
    with Progress(
        TextColumn("[bold yellow]⏳ Menunggu pengiriman berikutnya pada {task.description}"),
        BarColumn(),
        TimeRemainingColumn(),
        transient=True
    ) as progress:
        task = progress.add_task(f"{next_run.strftime('%Y-%m-%d 08:00 WIB')}", total=int(total_seconds))
        while datetime.now(JAKARTA_TZ) < next_run:
            elapsed = (datetime.now(JAKARTA_TZ) - (next_run - timedelta(seconds=total_seconds))).total_seconds()
            progress.update(task, completed=int(elapsed))
            time.sleep(1)
        progress.update(task, completed=int(total_seconds))
    logger.info("⏰ Waktu pengiriman berikutnya telah tiba!")

def main(randomize=False):
    logger.info("🟢 Fungsi `main()` dijalankan dari scheduler atau manual.")
    reset_sent_wallets()
    wallets = load_wallets(ignore_sent=True)
    if not wallets:
        logger.info("🚫 Tidak ada wallet untuk dikirim di wallets.csv.")
        console.print("[bold yellow]ℹ️ Tidak ada wallet valid di wallets.csv untuk diproses.[/bold yellow]")
        return False
    required_amount = sum([amt for _, amt in wallets])
    balance = check_balance()
    if balance < required_amount:
        logger.error(f"❌ Saldo tidak cukup! Dibutuhkan: {required_amount:.4f}, Tersedia: {balance:.4f}")
        console.print(f"[bold red]❌ Saldo tidak cukup! Dibutuhkan: {required_amount:.4f}, Tersedia: {balance:.4f}[/bold red]")
        return False
    logger.info(f"💰 Jumlah wallet yang akan diproses: {len(wallets)}")
    success = send_token_batch(wallets, randomize)
    return success

def ask_schedule_with_timeout():
    result = [None]

    def prompt_thread():
        console.print("[bold yellow]ℹ️ Apakah Anda ingin melanjutkan dengan pengiriman berjadwal setiap hari pukul 08:00 WIB?[/bold yellow]")
        result[0] = Prompt.ask("Pilih opsi (1=Ya, 0=Tidak)", choices=["0", "1"], default="1")

    thread = threading.Thread(target=prompt_thread)
    thread.start()
    thread.join(timeout=10)

    if result[0] is None:
        console.print("[bold yellow]⏰ Tidak ada respon dalam 10 detik, default ke pengiriman berjadwal secara acak.[/bold yellow]")
        return True
    return result[0] == "1"

def run_cli():
    while True:
        console.print("\n[bold cyan]=== MENU UTAMA ===[/bold cyan]", style="cyan")
        console.print(f"[bold yellow]Rentang Token Acak per Wallet: {MIN_TOKEN_AMOUNT} - {MAX_TOKEN_AMOUNT}[/bold yellow]")
        console.print(f"[bold yellow]Limit Harian Wallet: {DAILY_WALLET_LIMIT} alamat[/bold yellow]")
        console.print(f"[bold yellow]Gas Speed: fast (max {MAX_GAS_PRICE_GWEI} Gwei)[/bold yellow]")
        console.print("[1] Jalankan pengiriman token sekarang (berurutan)")
        console.print("[2] Jalankan pengiriman token sekarang (acak)")
        console.print("[3] Tampilkan log transaksi")
        console.print("[4] Jalankan mode penjadwalan (scheduler)")
        console.print("[5] Coba ulang alamat yang gagal")
        console.print("[0] Keluar")

        pilihan = Prompt.ask("Pilih opsi", choices=["0", "1", "2", "3", "4", "5"], default="0")

        if pilihan in ["1", "2"]:
            randomize = (pilihan == "2")
            success = main(randomize=randomize)
            if success:
                schedule_choice = ask_schedule_with_timeout()
                if schedule_choice:
                    schedule.every().day.at("08:00").do(main, randomize=True)
                    logger.info("🔌 Menjadwalkan pengiriman token setiap hari pukul 08:00 WIB (acak)")
                    console.print("[bold green]🔌 Scheduler aktif setiap hari pukul 08:00 WIB (acak)[/bold green]")
                    while True:
                        schedule.run_pending()
                        if not schedule.jobs:
                            show_progress_timer()
                            schedule.every().day.at("08:00").do(main, randomize=True)
                        time.sleep(1)
        elif pilihan == "3":
            check_logs()
        elif pilihan == "4":
            schedule.every().day.at("08:00").do(main, randomize=True)
            logger.info("🔌 Menjadwalkan pengiriman token setiap hari pukul 08:00 WIB (acak)")
            console.print("[bold green]🔌 Scheduler aktif setiap hari pukul 08:00 WIB (acak)[/bold green]")
            while True:
                schedule.run_pending()
                if not schedule.jobs:
                    show_progress_timer()
                    schedule.every().day.at("08:00").do(main, randomize=True)
                time.sleep(1)
        elif pilihan == "5":
            retry_failed_addresses()
        elif pilihan == "0":
            console.print("👋 Keluar dari program.", style="bold red")
            break

if __name__ == "__main__":
    run_cli()
