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
from rich.prompt import Prompt
from rich.text import Text
from rich.table import Table
from rich.live import Live
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore, Lock

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
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "2000"))

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

MAX_THREADS = 2  # Dikurangi untuk mengurangi konflik
RPC_SEMAPHORE = Semaphore(MAX_THREADS)
nonce_lock = Lock()
file_lock = Lock()
global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")


def get_next_nonce():
    with nonce_lock:
        global global_nonce
        pending_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
        if pending_nonce > global_nonce:
            global_nonce = pending_nonce
        current_nonce = global_nonce
        global_nonce += 1
        return current_nonce


def get_transaction_status_by_nonce(nonce):
    try:
        pending_block = w3.eth.get_block('pending', full_transactions=True)
        for tx in pending_block.transactions:
            if tx["from"].lower() == SENDER_ADDRESS.lower() and tx["nonce"] == nonce:
                return "pending"
        return "none"
    except Exception as e:
        logger.error(f"Gagal memeriksa status transaksi nonce {nonce}: {e}")
        return "error"


def get_gas_price(attempt=1, previous=None):
    try:
        latest_block = w3.eth.get_block('latest')
        base_fee = latest_block['baseFeePerGas'] / 10**9
        multiplier = 1.5 + (attempt - 1) * 1.0  # 1.5, 2.5, 3.5
        gas_price = base_fee * multiplier
        if previous and gas_price <= previous:
            gas_price = previous * 1.2  # Minimal 20% lebih tinggi
        return min(gas_price, MAX_GAS_PRICE_GWEI)
    except Exception as e:
        logger.error(f"❌ Gagal mengambil harga gas: {e}")
        return MAX_GAS_PRICE_GWEI


def cancel_transaction(nonce, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            gas_price = get_gas_price(attempt * 2)  # Gas lebih tinggi untuk pembatalan
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
            raw_tx = signed_tx.rawTransaction if hasattr(signed_tx, 'rawTransaction') else signed_tx.raw_transaction
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            console.print(f"[yellow]🚫 Membatalkan nonce {nonce} (percobaan {attempt}): {tx_hash.hex()[:10]}...[/yellow]")
            logger.info(f"Membatalkan transaksi nonce {nonce} dengan tx_hash: {tx_hash.hex()}")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return tx_hash
        except Exception as e:
            if "already known" in str(e):
                return None
            logger.error(f"Gagal membatalkan nonce {nonce} (percobaan {attempt}): {e}")
            time.sleep(2)
    return None


def send_worker(receiver, get_next_nonce_func, max_retries=3):
    if not Web3.is_address(receiver):
        logger.error(f"❌ Alamat tidak valid: {receiver}")
        console.print(f"[red]❌ Alamat tidak valid: {receiver}[/red]")
        return 0

    receiver = Web3.to_checksum_address(receiver)
    amount = round(random.uniform(MIN_TOKEN_AMOUNT, MAX_TOKEN_AMOUNT), 4)
    token_amount = int(amount * (10 ** TOKEN_DECIMALS))
    retry_delay = 3

    sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
    if sender_balance < amount:
        logger.error(f"❌ Saldo pengirim tidak cukup untuk {receiver}: {sender_balance} < {amount} token")
        console.print(f"[red]❌ Saldo pengirim tidak cukup untuk {receiver}: {sender_balance} < {amount}[/red]")
        return 0

    for attempt in range(1, max_retries + 1):
        try:
            gas_price = get_gas_price(attempt)
            nonce = get_next_nonce_func()
            logger.info(f"Memulai transaksi ke {receiver} | Nonce: {nonce} | Jumlah: {amount} token | Harga Gas: {gas_price:.1f} gwei")
            console.print(f"[blue]🧾 TX ke {receiver} | Nonce: {nonce} | Harga Gas: {gas_price:.1f} gwei[/blue]")
            time.sleep(random.uniform(0.4, 1.2))

            tx = token_contract.functions.transfer(receiver, token_amount).build_transaction({
                'from': SENDER_ADDRESS,
                'nonce': nonce,
                'gas': 100000,
                'gasPrice': w3.to_wei(gas_price, 'gwei'),
                'chainId': w3.eth.chain_id
            })

            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            raw_tx = getattr(signed_tx, 'rawTransaction', getattr(signed_tx, 'raw_transaction', None))
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            logger.info(f"Transaksi dikirim ke {receiver} | TX Hash: {tx_hash.hex()} | Menunggu konfirmasi...")
            time.sleep(random.uniform(2, 4))
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
                msg = f"✅ Berhasil mengirim {amount} token ke {receiver} | TX: {tx_hash.hex()}"
                logger.info(f"{msg} | Gas Used: {receipt.gasUsed}")
                console.print(msg)
                with file_lock:
                    with open(SENT_FILE, "a") as f:
                        f.write(f"{receiver}\n")
                    with open(TRANSACTION_LOG, "a") as logf:
                        logf.write(f"{datetime.now(pytz.timezone('Asia/Jakarta'))} | {receiver} | {amount} | {tx_hash.hex()} | Gas Used: {receipt.gasUsed}\n")
                return amount
            else:
                raise Exception("Transaksi gagal (status != 1)")

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Percobaan {attempt} gagal mengirim ke {receiver}: {error_msg}")
            console.print(f"[red]❌ Percobaan {attempt} gagal mengirim ke {receiver}: {error_msg}[/red]")
            if "replacement transaction underpriced" in error_msg and attempt < max_retries:
                logger.warning(f"⚠️ Harga gas terlalu rendah untuk nonce {nonce}, mencoba lagi dengan gas lebih tinggi")
                continue
            time.sleep(retry_delay)
            if attempt == max_retries:
                logger.error(f"❌ Gagal mengirim ke {receiver} setelah {max_retries} percobaan")
                cancel_transaction(nonce)
                with nonce_lock:
                    global global_nonce
                    global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
                    logger.warning("🔁 Nonce di-reset karena kegagalan transaksi")
    return 0


if __name__ == "__main__":
    console.print(Panel("[bold cyan]🚀 Memulai pengiriman token...[/bold cyan]"))

    sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
    logger.info(f"Saldo pengirim awal: {sender_balance} token")
    if sender_balance < MAX_TOTAL_SEND:
        logger.error(f"❌ Saldo pengirim tidak cukup: {sender_balance} < {MAX_TOTAL_SEND}")
        console.print(f"[red]❌ Saldo pengirim tidak cukup: {sender_balance} < {MAX_TOTAL_SEND}[/red]")
        exit()

    sent_wallets = set()
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE, "r") as f:
            sent_wallets = set(line.strip() for line in f.readlines())

    with open(CSV_FILE, "r") as f:
        reader = csv.reader(f)
        wallets = [
            line[0].strip()
            for line in reader
            if line and Web3.is_address(line[0].strip()) and line[0].strip() not in sent_wallets
        ]
        if not wallets:
            logger.error("❌ Tidak ada alamat dompet yang valid di wallets.csv")
            console.print("[red]❌ Tidak ada alamat dompet yang valid di wallets.csv[/red]")
            exit()
    logger.info(f"Jumlah dompet yang akan diproses: {min(len(wallets), DAILY_WALLET_LIMIT)}")

    random.shuffle(wallets)
    total_sent = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Mengirim token...", total=min(len(wallets), DAILY_WALLET_LIMIT))
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = []
            for receiver in wallets[:DAILY_WALLET_LIMIT]:
                if total_sent >= MAX_TOTAL_SEND:
                    logger.warning("⚠️ Batas maksimum total pengiriman tercapai")
                    break
                futures.append(executor.submit(send_worker, receiver, get_next_nonce))
            for future in as_completed(futures):
                sent = future.result()
                total_sent += sent
                sender_balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
                logger.info(f"Progres sementara: Total token dikirim = {total_sent} | Saldo pengirim tersisa: {sender_balance} token")
                progress.advance(task)
                time.sleep(0.3)

    logger.info(f"Selesai! Total token dikirim: {total_sent}")
    console.print(Panel(f"[green]✅ Selesai! Total token dikirim: {total_sent}[/green]"))
