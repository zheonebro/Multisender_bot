import os
import random
import time
import logging
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from web3 import Web3
import csv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, SpinnerColumn
from rich.live import Live
from rich.text import Text
from rich.table import Table
from rich import box
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore, Lock
from web3.exceptions import Web3RPCError, TransactionNotFound

# Setup logging
logging.basicConfig(
    filename="runtime_logs/runtime.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="[%Y-%m-%d %H:%M:%S]"
)
logger = logging.getLogger("bot")
console = Console()

# Load config
load_dotenv()
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
if PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = PRIVATE_KEY[2:]
SENDER_ADDRESS = Web3.to_checksum_address(os.getenv("SENDER_ADDRESS"))
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("TOKEN_CONTRACT"))
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "2"))  # Default ke 2 Gwei untuk Sepolia
MAX_TX_FEE_ETH = 0.001  # Batas biaya transaksi maksimum (dalam ETH/TEA)

MIN_TOKEN_AMOUNT = 10.0
MAX_TOKEN_AMOUNT = 50.0
DAILY_WALLET_LIMIT = 200
MAX_TOTAL_SEND = 1000  # Token
CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"

# Tetapkan waktu log saat program mulai
JAKARTA_TZ = pytz.timezone('Asia/Jakarta')
START_TIME = datetime.now(JAKARTA_TZ).strftime('%Y%m%d_%H%M%S')
TRANSACTION_LOG = f"runtime_logs/transactions_{START_TIME}.log"

# Connect to Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan!")
    console.print("[bold red]❌ Gagal terhubung ke jaringan![/bold red]")
    exit()

# Token contract
TOKEN_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "outputs": [{"name": "", "type": "bool"}]
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}]
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_owner", "type": "address"}],
        "outputs": [{"name": "balance", "type": "uint256"}]
    }
]
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=TOKEN_ABI)
TOKEN_DECIMALS = token_contract.functions.decimals().call()

MAX_THREADS = 2
RPC_SEMAPHORE = Semaphore(MAX_THREADS)
nonce_lock = Lock()
file_lock = Lock()
global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")

def get_dynamic_max_gas_price():
    """Menghitung batas harga gas maksimum secara dinamis dengan batas realistis."""
    try:
        fee_history = w3.eth.fee_history(10, "latest", reward_percentiles=[50])  # Gunakan persentil 50 untuk stabilitas
        base_fee = max(fee_history["baseFeePerGas"]) / 10**9
        priority_fee = w3.eth.max_priority_fee / 10**9
        dynamic_max = (base_fee + priority_fee) * 1.05  # Margin 1.05x untuk Sepolia
        # Batasi maksimum pada 5 Gwei untuk testnet Sepolia
        return min(dynamic_max, 5)
    except Exception as e:
        logger.warning(f"⚠️ Gagal menghitung max gas dinamis: {e}. Menggunakan default.")
        return min(w3.eth.gas_price / 10**9 * 1.05, 5)  # Fallback realistis

def get_gas_price(attempt=1, max_gas_price_gwei=None):
    """Menghitung harga gas dengan batas biaya transaksi."""
    try:
        base_fee = w3.eth.fee_history(1, "latest", reward_percentiles=[50])["baseFeePerGas"][-1] / 10**9
        priority_fee = w3.eth.max_priority_fee / 10**9
        multiplier = 1.05 + (attempt - 1) * 0.05  # Multiplier rendah untuk Sepolia
        gas_price = (base_fee + priority_fee) * multiplier
        effective_max = max_gas_price_gwei if max_gas_price_gwei > 0 else get_dynamic_max_gas_price()
        # Pastikan biaya transaksi tidak melebihi MAX_TX_FEE_ETH
        max_gas_price_from_fee = (MAX_TX_FEE_ETH * 10**18) / 65000 / 10**9  # Gas limit 65,000
        return min(gas_price, effective_max, max_gas_price_from_fee)
    except Exception as e:
        logger.warning(f"⚠️ Gagal mengambil harga gas: {e}. Menggunakan default.")
        default_gas_price = w3.eth.gas_price / 10**9
        effective_max = max_gas_price_gwei if max_gas_price_gwei > 0 else get_dynamic_max_gas_price()
        return min(default_gas_price * 1.05, effective_max, 5)

def get_next_nonce():
    with nonce_lock:
        global global_nonce
        current_nonce = global_nonce
        global_nonce += 1
        return current_nonce

def refresh_nonce():
    with nonce_lock:
        global global_nonce
        try:
            global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
            logger.info(f"🔄 Nonce di-refresh ke: {global_nonce}")
        except Web3RPCError as e:
            logger.error(f"❌ Gagal refresh nonce: {e}")
            time.sleep(5)
            global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")

def cancel_transaction(nonce, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            gas_price = get_gas_price(attempt=2, max_gas_price_gwei=MAX_GAS_PRICE_GWEI)
            tx = {
                'from': SENDER_ADDRESS,
                'to': SENDER_ADDRESS,
                'value': 0,
                'nonce': nonce,
                'gas': 21000,
                'gasPrice': w3.to_wei(gas_price, 'gwei'),
                'chainId': w3.eth.chain_id
            }
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            console.print(f"[yellow]🚫 Membatalkan nonce {nonce}: {tx_hash.hex()[:10]}...[/yellow]")
            logger.info(f"Membatalkan transaksi nonce {nonce} dengan tx_hash: {tx_hash.hex()}")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return tx_hash
        except Web3RPCError as e:
            if "capacity exceeded" in str(e):
                logger.warning(f"⚠️ Kapasitas node penuh saat membatalkan nonce {nonce}. Mencoba lagi ({attempt}/{max_attempts})")
                time.sleep(2 * attempt)
                continue
            logger.error(f"❌ Gagal membatalkan nonce {nonce}: {e}")
            return None
        except Exception as e:
            if "already known" in str(e):
                return None
            logger.error(f"❌ Gagal membatalkan nonce {nonce}: {e}")
            time.sleep(2)
    return None

def display_initial_status():
    try:
        sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
        gas_price = get_gas_price(max_gas_price_gwei=MAX_GAS_PRICE_GWEI)
        eth_balance = w3.eth.get_balance(SENDER_ADDRESS) / 10**18
        estimated_gas_cost = (65000 * w3.to_wei(gas_price, 'gwei')) / 10**18  # Update ke 65,000
        dynamic_max = get_dynamic_max_gas_price()
        
        table = Table(title="Status Awal", box=box.ROUNDED, style="cyan")
        table.add_column("Parameter", style="bold magenta")
        table.add_column("Nilai", style="bold green")
        
        table.add_row("Saldo Token", f"{sender_balance:.4f} token")
        table.add_row("Harga Gas Saat Ini", f"{gas_price:.2f} Gwei")
        table.add_row("Estimasi Biaya Gas per TX", f"{estimated_gas_cost:.6f} TEA")
        table.add_row("Saldo TEA Pengirim", f"{eth_balance:.4f} TEA")
        table.add_row("Batas Gas Maksimum", f"{MAX_GAS_PRICE_GWEI if MAX_GAS_PRICE_GWEI > 0 else dynamic_max:.2f} Gwei")
        table.add_row("Batas Biaya TX Maksimum", f"{MAX_TX_FEE_ETH:.2f} TEA")
        
        console.print(Panel(table, title="[bold cyan]📊 Informasi Awal[/bold cyan]", border_style="cyan"))
        
        if estimated_gas_cost > MAX_TX_FEE_ETH:
            console.print(f"[red]❌ Estimasi biaya gas ({estimated_gas_cost:.6f} ETH) melebihi batas node ({MAX_TX_FEE_ETH} ETH). Transaksi mungkin gagal![/red]")
            logger.error(f"Estimasi biaya gas ({estimated_gas_cost:.6f} ETH) > batas node ({MAX_TX_FEE_ETH} ETH)")
        elif MAX_GAS_PRICE_GWEI > 0 and MAX_GAS_PRICE_GWEI < gas_price:
            console.print(f"[yellow]⚠️ MAX_GAS_PRICE_GWEI ({MAX_GAS_PRICE_GWEI} Gwei) lebih rendah dari harga gas jaringan ({gas_price:.2f} Gwei).[/yellow]")
            logger.warning(f"MAX_GAS_PRICE_GWEI ({MAX_GAS_PRICE_GWEI}) < harga gas jaringan ({gas_price:.2f})")
        
        logger.info(f"Status awal - Saldo Token: {sender_balance}, Gas Price: {gas_price} Gwei, ETH Balance: {eth_balance}, Max Gas: {MAX_GAS_PRICE_GWEI if MAX_GAS_PRICE_GWEI > 0 else dynamic_max} Gwei, Max TX Fee: {MAX_TX_FEE_ETH} ETH")
        
        return sender_balance, eth_balance
    except Exception as e:
        logger.error(f"❌ Gagal mengambil status awal: {e}")
        console.print(f"[red]❌ Gagal mengambil status awal: {e}[/red]")
        return 0, 0

def send_worker(receiver, get_next_nonce_func, max_retries=3):
    with RPC_SEMAPHORE:
        if not Web3.is_address(receiver):
            logger.error(f"❌ Alamat tidak valid: {receiver}")
            console.print(f"[red]❌ Alamat tidak valid: {receiver}[/red]")
            return 0

        receiver = Web3.to_checksum_address(receiver)
        amount = round(random.uniform(MIN_TOKEN_AMOUNT, MAX_TOKEN_AMOUNT), 4)
        token_amount = int(amount * (10 ** TOKEN_DECIMALS))

        sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
        if sender_balance < amount:
            logger.error(f"❌ Saldo pengirim tidak cukup untuk {receiver}: {sender_balance} < {amount} token")
            console.print(f"[red]❌ Saldo pengirim tidak cukup untuk {receiver}: {sender_balance} < {amount}[/red]")
            return 0

        eth_balance = w3.eth.get_balance(SENDER_ADDRESS) / 10**18
        gas_price = get_gas_price(max_gas_price_gwei=MAX_GAS_PRICE_GWEI)
        estimated_gas_cost = (65000 * w3.to_wei(gas_price, 'gwei')) / 10**18  # Update ke 65,000
        if eth_balance < estimated_gas_cost:
            logger.error(f"❌ Saldo ETH tidak cukup untuk gas: {eth_balance} < {estimated_gas_cost} ETH")
            console.print(f"[red]❌ Saldo ETH tidak cukup untuk gas: {eth_balance} < {estimated_gas_cost} ETH[/red]")
            return 0
        if estimated_gas_cost > MAX_TX_FEE_ETH:
            logger.error(f"❌ Biaya gas ({estimated_gas_cost:.6f} ETH) melebihi batas node ({MAX_TX_FEE_ETH} ETH)")
            console.print(f"[red]❌ Biaya gas ({estimated_gas_cost:.6f} ETH) melebihi batas node ({MAX_TX_FEE_ETH} ETH)[/red]")
            return 0

        for attempt in range(1, max_retries + 1):
            try:
                gas_price = get_gas_price(attempt=attempt, max_gas_price_gwei=MAX_GAS_PRICE_GWEI)
                nonce = get_next_nonce_func()
                logger.info(f"Memulai transaksi ke {receiver} | Nonce: {nonce} | Jumlah: {amount} token | Harga Gas: {gas_price:.1f} gwei")
                console.print(f"[blue]🧾 TX ke {receiver} | Nonce: {nonce} | Harga Gas: {gas_price:.1f} gwei[/blue]")
                time.sleep(random.uniform(0.5, 1.5))

                tx = token_contract.functions.transfer(receiver, token_amount).build_transaction({
                    'from': SENDER_ADDRESS,
                    'nonce': nonce,
                    'gas': 65000,  # Update ke 65,000
                    'gasPrice': w3.to_wei(gas_price, 'gwei'),
                    'chainId': w3.eth.chain_id
                })

                signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                logger.info(f"Transaksi dikirim ke {receiver} | TX Hash: {tx_hash.hex()} | Menunggu konfirmasi...")
                
                time.sleep(5)
                try:
                    w3.eth.get_transaction(tx_hash)
                except TransactionNotFound:
                    logger.warning(f"⚠️ Transaksi {tx_hash.hex()} tidak ditemukan di mempool. Mencoba ulang...")
                    cancel_transaction(nonce)
                    continue

                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

                if receipt.status == 1:
                    sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
                    msg = f"✅ Berhasil mengirim {amount} token ke {receiver} | TX: {tx_hash.hex()}"
                    logger.info(f"{msg} | Gas Used: {receipt.gasUsed}")
                    console.print(msg)
                    with file_lock:
                        with open(SENT_FILE, "a") as f:
                            f.write(f"{receiver}|{datetime.now(JAKARTA_TZ).strftime('%Y-%m-%d')}\n")
                        with open(TRANSACTION_LOG, "a") as logf:
                            logf.write(f"{datetime.now(JAKARTA_TZ)} | {receiver} | {amount} | {tx_hash.hex()} | Gas Used: {receipt.gasUsed}\n")
                    return amount
                else:
                    raise Exception("Transaksi gagal (status != 1)")

            except Web3RPCError as e:
                error_msg = str(e)
                logger.error(f"❌ Percobaan {attempt} gagal mengirim ke {receiver}: {error_msg}")
                console.print(f"[red]❌ Percobaan {attempt} gagal mengirim ke {receiver}: {error_msg}[/red]")
                if "transaction underpriced" in error_msg:
                    logger.info(f"⚠️ Harga gas terlalu rendah. Meningkatkan gas untuk percobaan berikutnya.")
                    continue
                if "exceeds the configured cap" in error_msg:
                    logger.error(f"❌ Biaya gas terlalu tinggi untuk node. Membatalkan percobaan.")
                    return 0
                if "capacity exceeded" in error_msg and attempt < max_retries:
                    time.sleep(2 * attempt)
                    continue
                if attempt == max_retries:
                    logger.error(f"❌ Gagal mengirim ke {receiver} setelah {max_retries} percobaan")
                    cancel_transaction(nonce)
                    refreshავ

def check_daily_quota():
    today = datetime.now(JAKARTA_TZ).date()
    sent_wallets = set()
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r") as f:
            for line in f:
                wallet, timestamp = line.strip().split("|") if "|" in line else (line.strip(), "1970-01-01")
                if datetime.strptime(timestamp, "%Y-%m-%d").date() == today:
                    sent_wallets.add(wallet)
    return len(sent_wallets) >= DAILY_WALLET_LIMIT, len(sent_wallets)

def get_next_reset_time():
    now = datetime.now(JAKARTA_TZ)
    next_day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return next_day

def countdown_to_next_day():
    next_reset = get_next_reset_time()
    animation_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    colors = ["cyan", "green", "yellow", "magenta"]
    frame_idx = 0
    color_idx = 0

    with Live(console=console, refresh_per_second=4) as live:
        while True:
            now = datetime.now(JAKARTA_TZ)
            time_left = next_reset - now
            if time_left.total_seconds() <= 0:
                break

            hours, remainder = divmod(int(time_left.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)

            spinner = animation_frames[frame_idx]
            color = colors[color_idx]
            frame_idx = (frame_idx + 1) % len(animation_frames)
            if frame_idx == 0:
                color_idx = (color_idx + 1) % len(colors)

            countdown_text = Text(
                f"{spinner} Menunggu pengiriman hari berikutnya...\n"
                f"Waktu Tersisa: {hours:02d}:{minutes:02d}:{seconds:02d}",
                style=f"bold {color}"
            )
            panel = Panel(
                countdown_text,
                title="[blink]⏳ Idle Time[/blink]",
                subtitle=f"Reset pada: {next_reset.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                border_style=color,
                box=box.ROUNDED,
                padding=(1, 2),
                expand=False
            )
            live.update(panel)
            time.sleep(0.25)

    console.print("[bold green]⏰ Waktu reset tercapai! Memulai pengiriman baru...[/bold green]")

if __name__ == "__main__":
    while True:
        console.print(Panel("[bold cyan]🚀 Memulai pengiriman token...[/bold cyan]"))

        sender_balance, eth_balance = display_initial_status()
        if sender_balance == 0 or eth_balance == 0:
            logger.error("❌ Tidak dapat melanjutkan karena gagal mengambil status awal")
            exit()

        quota_full, sent_count = check_daily_quota()
        logger.info(f"Memeriksa kuota harian: {sent_count}/{DAILY_WALLET_LIMIT} dompet telah diproses hari ini")
        if quota_full:
            console.print(f"[yellow]⚠️ Kuota harian ({DAILY_WALLET_LIMIT} dompet) telah tercapai![/yellow]")
            logger.info(f"Kuota harian tercapai ({sent_count}/{DAILY_WALLET_LIMIT}). Menunggu reset harian berikutnya.")
            countdown_to_next_day()
            continue

        if sender_balance < MAX_TOTAL_SEND:
            logger.error(f"❌ Saldo pengirim tidak cukup: {sender_balance} < {MAX_TOTAL_SEND}")
            console.print(f"[red]❌ Saldo pengirim tidak cukup: {sender_balance} < {MAX_TOTAL_SEND}[/red]")
            exit()

        with open(CSV_FILE, "r") as f:
            reader = csv.reader(f)
            all_wallets = [line[0].strip() for line in reader if line and Web3.is_address(line[0].strip())]
            if not all_wallets:
                logger.error("❌ Tidak ada alamat dompet yang valid di wallets.csv")
                console.print("[red]❌ Tidak ada alamat dompet yang valid di wallets.csv[/red]")
                exit()

        sent_wallets = set()
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r") as f:
                for line in f:
                    wallet, timestamp = line.strip().split("|") if "|" in line else (line.strip(), "1970-01-01")
                    if datetime.strptime(timestamp, "%Y-%m-%d").date() == datetime.now(JAKARTA_TZ).date():
                        sent_wallets.add(wallet)

        wallets_to_process = [w for w in all_wallets if w not in sent_wallets]
        logger.info(f"Jumlah dompet yang akan diproses hari ini: {min(len(wallets_to_process), DAILY_WALLET_LIMIT - sent_count)}")

        if not wallets_to_process:
            logger.info("✅ Semua wallet dalam daftar telah diproses hari ini.")
            console.print("[green]✅ Semua wallet dalam daftar telah diproses hari ini![/green]")
            countdown_to_next_day()
            continue

        random.shuffle(wallets_to_process)
        total_sent = 0
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Mengirim token...", total=min(len(wallets_to_process), DAILY_WALLET_LIMIT - sent_count))
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = []
                for receiver in wallets_to_process[:DAILY_WALLET_LIMIT - sent_count]:
                    if total_sent >= MAX_TOTAL_SEND:
                        logger.warning("⚠️ Batas maksimum total pengiriman tercapai")
                        break
                    futures.append(executor.submit(send_worker, receiver, get_next_nonce))
                for future in as_completed(futures):
                    try:
                        sent = future.result()
                        total_sent += sent
                        sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
                        logger.info(f"Progres sementara: Total token dikirim = {total_sent} | Saldo pengirim tersisa: {sender_balance} token")
                        progress.advance(task)
                    except Exception as e:
                        logger.error(f"❌ Error di thread: {e}")
                        console.print(f"[red]❌ Error di thread: {e}[/red]")
                    time.sleep(0.5)

        logger.info(f"Selesai! Total token dikirim hari ini: {total_sent}")
        console.print(Panel(
            f"[green]✅ Selesai Hari Ini!\n"
            f"Total Token Dikirim: {total_sent}\n"
            f"Dompet Diproses: {sent_count + len(futures)}\n"
            f"Saldo Tersisa: {sender_balance:.4f} token[/green]",
            title="Ringkasan Harian",
            border_style="green"
        ))

        remaining_wallets = [w for w in all_wallets if w not in sent_wallets]
        if not remaining_wallets or total_sent >= MAX_TOTAL_SEND or sent_count + len(futures) >= DAILY_WALLET_LIMIT:
            logger.info("✅ Pengiriman harian selesai atau kuota tercapai. Menunggu hari berikutnya.")
            console.print("[cyan]📅 Menunggu reset harian untuk pengiriman ulang...[/cyan]")
            countdown_to_next_day()
        else:
            logger.info("🔄 Melanjutkan pengiriman ke wallet tersisa hari ini.")
            console.print("[cyan]🔄 Melanjutkan pengiriman ke wallet tersisa...[/cyan]")
