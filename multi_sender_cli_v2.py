import csv
import os
import random
import time
from datetime import datetime, time as dt_time, timedelta
import logging
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich import box
import web3
import schedule
import pytz
import sys
import requests
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.prompt import Prompt, IntPrompt
import threading

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
transaction_log_path = os.path.join(log_dir, f"transactions_{datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y%m%d_%H%M%S')}.log")

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
stream_handler.setLevel(logging.WARNING)

formatter = JakartaFormatter(fmt="%(asctime)s %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]")
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)

console.print("[bold green]🟢 Bot dimulai. Log detail tersedia di runtime.log[/bold green]")
logger.info("🕒 Logging timezone aktif: Asia/Jakarta")
logger.info(f"📜 Log transaksi akan disimpan di: {transaction_log_path}")

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RAW_SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_RAW = os.getenv("TOKEN_CONTRACT")
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "100"))
EXPLORER_URL = "https://sepolia.tea.xyz/tx/"

IDLE_SECONDS = int(os.getenv("IDLE_SECONDS", 5))  # Jeda antar pengiriman dikurangi ke 5 detik
MIN_TOKEN_AMOUNT = 10.0
MAX_TOKEN_AMOUNT = 50.0
DAILY_WALLET_LIMIT = int(os.getenv("DAILY_WALLET_LIMIT", 200))  # Maksimum 200 wallet per hari

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

logger.info(f"⚙️ Konfigurasi: MIN_TOKEN_AMOUNT={MIN_TOKEN_AMOUNT}, MAX_TOKEN_AMOUNT={MAX_TOKEN_AMOUNT}, DAILY_WALLET_LIMIT={DAILY_WALLET_LIMIT}, IDLE_SECONDS={IDLE_SECONDS}, MAX_GAS_PRICE_GWEI={MAX_GAS_PRICE_GWEI}")

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

failed_addresses = []  # Untuk retry manual

# Fungsi Gas Price
def get_sepolia_tea_gas_price(multiplier=1.0, previous_gas_price=None):
    url = "https://sepolia.tea.xyz/api/v1/gas-price-oracle"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        gas_price_gwei = float(data.get("fast", 0)) * 1.2 * multiplier
        if previous_gas_price and gas_price_gwei <= previous_gas_price:
            gas_price_gwei = previous_gas_price * 1.1  # Minimal 10% lebih tinggi
        return min(gas_price_gwei, MAX_GAS_PRICE_GWEI)
    except requests.RequestException as e:
        logger.error(f"❌ Gagal mengambil gas price dari Sepolia TEA: {e}")
        network_gas_price = w3.eth.gas_price / 10**9 * 1.2 * multiplier
        if previous_gas_price and network_gas_price <= previous_gas_price:
            network_gas_price = previous_gas_price * 1.1
        return min(network_gas_price, MAX_GAS_PRICE_GWEI)

# Fungsi Pembatalan Transaksi
def cancel_transaction(tx_hash, nonce):
    cancel_tx = {
        'from': SENDER_ADDRESS,
        'to': SENDER_ADDRESS,
        'value': 0,
        'nonce': nonce,
        'gas': 21000,
        'gasPrice': w3.to_wei(get_sepolia_tea_gas_price(multiplier=5.0), 'gwei')
    }
    signed_cancel_tx = w3.eth.account.sign_transaction(cancel_tx, PRIVATE_KEY)
    try:
        cancel_hash = w3.eth.send_raw_transaction(signed_cancel_tx.raw_transaction)
        logger.info(f"🚫 Membatalkan transaksi {tx_hash} dengan {cancel_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(cancel_hash, timeout=120)
        logger.info(f"✅ Pembatalan {cancel_hash.hex()} dikonfirmasi")
        return cancel_hash
    except Exception as e:
        logger.error(f"❌ Gagal membatalkan transaksi {tx_hash}: {e}")
        return None

def check_balance():
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call()
    balance_in_tokens = balance / (10 ** TOKEN_DECIMALS)
    logger.info(f"💰 Saldo token pengirim: {balance_in_tokens:.4f}")
    return balance_in_tokens

def load_wallets(ignore_sent=False, limit=DAILY_WALLET_LIMIT):
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

def display_transaction_logs(log_file=None):
    target_log_file = log_file if log_file else transaction_log_path
    
    if not os.path.exists(target_log_file):
        console.print(f"📬 Belum ada transaksi yang dicatat di {target_log_file}.", style="yellow")
        return

    table = Table(title=f"📌 LOG TRANSAKSI TOKEN ({os.path.basename(target_log_file)})", box=box.SIMPLE_HEAVY)
    table.add_column("No", justify="center", style="dim")
    table.add_column("Waktu", style="dim", width=20)
    table.add_column("Alamat Tujuan", style="cyan")
    table.add_column("Jumlah", justify="right", style="green")
    table.add_column("Status", style="bold")
    table.add_column("Explorer Link", overflow="fold")

    sukses = gagal = 0
    total_token = 0.0

    with open(target_log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        for idx, line in enumerate(lines, 1):
            parts = line.strip().split(",")
            if len(parts) >= 5:
                waktu, alamat, jumlah, status, *detail = parts
                detail = ",".join(detail)
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
    logger.info("📜 Menampilkan opsi untuk memilih log transaksi...")
    log_files = [f for f in os.listdir(log_dir) if f.startswith("transactions_") and f.endswith(".log")]
    if not log_files:
        console.print("[bold yellow]ℹ️ Tidak ada file log transaksi di folder runtime_logs.[/bold yellow]")
        return

    log_files.sort(reverse=True)
    console.print("\n[bold cyan]=== PILIH FILE LOG TRANSAKSI ===[/bold cyan]")
    for idx, log_file in enumerate(log_files, 1):
        console.print(f"[{idx}] {log_file}")
    console.print("[0] Kembali ke menu utama")

    pilihan = IntPrompt.ask("Pilih nomor file log", default=1, show_default=True)
    if pilihan == 0:
        return
    elif 1 <= pilihan <= len(log_files):
        selected_log = os.path.join(log_dir, log_files[pilihan - 1])
        console.print(f"[bold green]📜 Menampilkan log dari: {selected_log}[/bold green]")
        display_transaction_logs(selected_log)
    else:
        console.print("[bold red]❌ Pilihan tidak valid![/bold red]")

def _send_token(to_address, amount, remaining_wallets, attempt=1, previous_gas_price=None):
    from_address = SENDER_ADDRESS
    to_address = web3.Web3.to_checksum_address(to_address)
    scaled_amount = int(amount * (10 ** TOKEN_DECIMALS))

    gas_multiplier = 1.0 + (attempt - 1) * 0.5
    tea_gas_price = get_sepolia_tea_gas_price(multiplier=gas_multiplier, previous_gas_price=previous_gas_price)
    gas_price_to_use = min(tea_gas_price, MAX_GAS_PRICE_GWEI)

    try:
        gas_estimate = token_contract.functions.transfer(to_address, scaled_amount).estimate_gas({'from': from_address})
        gas_limit = int(gas_estimate * 1.5)
    except Exception as e:
        logger.error(f"❌ Gagal mengestimasi gas untuk {to_address} (attempt {attempt}): {e}")
        if remaining_wallets:
            new_addr, new_amt = remaining_wallets.pop(0)
            logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
            return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        raise Exception(f"Gagal estimasi gas dan tidak ada alamat pengganti: {e}")

    nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
    tx = token_contract.functions.transfer(to_address, scaled_amount).build_transaction({
        'from': from_address,
        'nonce': nonce,
        'gas': gas_limit,
        'gasPrice': w3.to_wei(gas_price_to_use, 'gwei')
    })

    logger.info(f"ℹ️ Tx Details (attempt {attempt}): {tx}")
    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    try:
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"📤 Transaksi dikirim: {tx_hash.hex()}")

        tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if tx_receipt.status != 1:
            logger.error(f"❌ Transaksi {tx_hash.hex()} gagal di chain: Status {tx_receipt.status}")
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception(f"Transaksi gagal: Status {tx_receipt.status}")
        logger.info(f"✅ Transaksi {tx_hash.hex()} dikonfirmasi")
        return tx_hash.hex()
    except web3.exceptions.TimeExhausted:
        logger.error(f"⏰ Transaksi {tx_hash.hex()} timeout setelah 120 detik (attempt {attempt})")
        cancel_hash = cancel_transaction(tx_hash.hex(), nonce)
        if cancel_hash:
            logger.info(f"✅ Transaksi lama dibatalkan, lanjut ke alamat berikutnya")
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception("Timeout dan tidak ada alamat pengganti")
        else:
            logger.error(f"❌ Gagal membatalkan transaksi {tx_hash.hex()}, tunggu hingga drop")
            time.sleep(60)  # Dikurangi dari 300 detik ke 60 detik
            updated_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
            if updated_nonce > nonce:
                logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            if attempt < 2:
                logger.info(f"🔄 Mencoba lagi dengan gas lebih tinggi untuk {to_address}")
                return _send_token(to_address, amount, remaining_wallets, attempt + 1, previous_gas_price=gas_price_to_use)
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception("Timeout dan gagal membatalkan transaksi")
    except ValueError as e:
        if "replacement transaction underpriced" in str(e):
            logger.error(f"❌ Replacement underpriced untuk {tx_hash.hex()} (attempt {attempt})")
            cancel_hash = cancel_transaction(tx_hash.hex(), nonce)
            if cancel_hash:
                logger.info(f"✅ Transaksi lama dibatalkan, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            else:
                logger.error(f"❌ Gagal membatalkan transaksi {tx_hash.hex()}, tunggu hingga drop")
                time.sleep(60)  # Dikurangi dari 300 detik ke 60 detik
                updated_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
                if updated_nonce > nonce:
                    logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi, lanjut ke alamat berikutnya")
                    if remaining_wallets:
                        new_addr, new_amt = remaining_wallets.pop(0)
                        logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                        return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
                if attempt < 2:
                    logger.info(f"🔄 Mencoba lagi dengan gas lebih tinggi untuk {to_address}")
                    return _send_token(to_address, amount, remaining_wallets, attempt + 1, previous_gas_price=gas_price_to_use)
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception("Replacement underpriced dan gagal membatalkan transaksi")
        elif "nonce too low" in str(e):
            logger.error(f"❌ Nonce terlalu rendah untuk {tx_hash.hex()}, sinkronisasi ulang")
            time.sleep(5)  # Dikurangi dari 10 detik ke 5 detik
            updated_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
            logger.info(f"ℹ️ Nonce diperbarui ke: {updated_nonce}")
            return _send_token(to_address, amount, remaining_wallets, attempt=1)
        elif "transaction already imported" in str(e):
            logger.error(f"❌ Transaksi {tx_hash.hex()} sudah ada di mempool, tunggu")
            time.sleep(60)  # Dikurangi dari 300 detik ke 60 detik
            updated_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
            if updated_nonce > nonce:
                logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        raise
    finally:
        time.sleep(5)  # Jeda sinkronisasi dikurangi dari 10 detik ke 5 detik

def send_token(to_address, amount, remaining_wallets):
    try:
        tx_hash = _send_token(to_address, amount, remaining_wallets)
        logger.info(f"✅ Token terkirim ke {to_address} | Amount: {amount:.4f} | TxHash: {tx_hash}")
        log_transaction(to_address, amount, "SUCCESS", tx_hash)
        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address}\n")
        return True, amount
    except Exception as e:
        logger.error(f"❌ Gagal mengirim ke {to_address}: {e}")
        log_transaction(to_address, amount, "FAILED", str(e))
        failed_addresses.append((to_address, amount))
        return False, 0

def reset_sent_wallets():
    try:
        with open(SENT_FILE, "w") as f:
            f.write("")
        logger.info("🔄 File sent_wallets.txt telah direset secara otomatis.")
        return True
    except Exception as e:
        logger.error(f"❌ Gagal mereset sent_wallets.txt: {e}")
        return False

def send_tokens(wallets, randomize=False):
    if randomize:
        random.shuffle(wallets)
        logger.info("🔀 Daftar wallet diacak untuk pengiriman acak.")
    else:
        logger.info("📋 Daftar wallet akan diproses secara berurutan.")
    
    total_wallets_sent = 0
    total_token_sent = 0.0
    remaining_wallets = wallets.copy()

    console.print(f"Memproses pengiriman ke maksimum {DAILY_WALLET_LIMIT} wallet...")
    logger.info(f"🚀 Memulai pengiriman ke maksimum {DAILY_WALLET_LIMIT} wallet")
    
    with Progress(
        TextColumn("[bold green]Mengirim token..."),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
    ) as progress:
        task = progress.add_task("", total=min(len(wallets), DAILY_WALLET_LIMIT))
        for addr, amt in wallets:
            if total_wallets_sent >= DAILY_WALLET_LIMIT:
                logger.warning(f"🚘 Mencapai batas harian {DAILY_WALLET_LIMIT} wallet.")
                console.print(f"[bold red]ℹ️ Limit harian wallet tercapai: {total_wallets_sent}/{DAILY_WALLET_LIMIT} alamat telah dikirim hari ini.[/bold red]")
                break
            
            success, amount = send_token(addr, amt, remaining_wallets)
            if success:
                total_wallets_sent += 1
                total_token_sent += amount
            progress.advance(task)
            
            console.print(f"⏳ Menunggu {IDLE_SECONDS} detik sebelum pengiriman berikutnya...")
            logger.info(f"⏳ Menunggu {IDLE_SECONDS} detik sebelum pengiriman berikutnya...")
            time.sleep(IDLE_SECONDS)

    logger.info(f"✅ Pengiriman selesai. Total wallet dikirim: {total_wallets_sent}, Total token: {total_token_sent:.4f}")
    console.print(f"[bold green]📦 Total wallet berhasil dikirim: {total_wallets_sent} | Total token: {total_token_sent:.4f}[/bold green]")
    return total_wallets_sent

def retry_failed_addresses():
    global failed_addresses
    if not failed_addresses:
        logger.info("✅ Tidak ada alamat yang gagal untuk dicoba ulang.")
        console.print("[bold green]✅ Tidak ada alamat yang gagal untuk dicoba ulang.[/bold green]")
        return
    logger.info(f"🔄 Mencoba ulang {len(failed_addresses)} alamat yang gagal...")
    total_sent = send_tokens(failed_addresses)
    if total_sent == len(failed_addresses):  # Jika semua selesai
        failed_addresses = []

def get_next_schedule_time():
    now = datetime.now(JAKARTA_TZ)
    next_run = datetime.combine(now.date(), dt_time(8, 0), tzinfo=JAKARTA_TZ)
    if now >= next_run:
        next_run += timedelta(days=1)
    return next_run

def show_progress_timer(next_run):
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

def schedule_next_day(randomize):
    next_run = get_next_schedule_time()
    schedule.clear()  # Bersihkan jadwal sebelumnya
    schedule.every().day.at("08:00").do(main, randomize=randomize)
    logger.info(f"🔌 Pengiriman berikutnya dijadwalkan pada {next_run.strftime('%Y-%m-%d 08:00 WIB')} {'(acak)' if randomize else '(berurutan)'}")
    console.print(f"[bold green]🔌 Pengiriman berikutnya dijadwalkan pada {next_run.strftime('%Y-%m-%d 08:00 WIB')} {'(acak)' if randomize else '(berurutan)'} [/bold green]")
    return next_run

def main(randomize=False):
    logger.info("🟢 Fungsi `main()` dijalankan dari scheduler atau manual.")
    reset_sent_wallets()
    wallets = load_wallets(ignore_sent=True, limit=DAILY_WALLET_LIMIT)
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
    total_sent = send_tokens(wallets, randomize)
    
    # Jadwalkan otomatis untuk hari berikutnya setelah pengiriman selesai
    next_run = schedule_next_day(randomize)
    console.print(f"[bold yellow]⏳ Menunggu hingga {next_run.strftime('%Y-%m-%d 08:00 WIB')}...[/bold yellow]")
    show_progress_timer(next_run)
    return True

def run_cli():
    while True:
        console.print("\n[bold cyan]=== MENU UTAMA ===[/bold cyan]", style="cyan")
        console.print(f"[bold yellow]Rentang Token Acak per Wallet: {MIN_TOKEN_AMOUNT} - {MAX_TOKEN_AMOUNT}[/bold yellow]")
        console.print(f"[bold yellow]Limit Harian Wallet: {DAILY_WALLET_LIMIT} alamat[/bold yellow]")
        console.print(f"[bold yellow]Gas Speed: fast (max {MAX_GAS_PRICE_GWEI} Gwei)[/bold yellow]")
        console.print(f"[bold yellow]Log Transaksi Saat Ini: {os.path.basename(transaction_log_path)}[/bold yellow]")
        console.print("[1] Jalankan pengiriman token sekarang (berurutan)")
        console.print("[2] Jalankan pengiriman token sekarang (acak)")
        console.print("[3] Tampilkan log transaksi (pilih file)")
        console.print("[4] Coba ulang alamat yang gagal")
        console.print("[0] Keluar")

        pilihan = Prompt.ask("Pilih opsi", choices=["0", "1", "2", "3", "4"], default="0")

        if pilihan in ["1", "2"]:
            randomize = (pilihan == "2")
            success = main(randomize=randomize)
            if success:
                while True:
                    schedule.run_pending()
                    time.sleep(1)
        elif pilihan == "3":
            check_logs()
        elif pilihan == "4":
            retry_failed_addresses()
        elif pilihan == "0":
            console.print("👋 Keluar dari program.", style="bold red")
            break

if __name__ == "__main__":
    run_cli()
