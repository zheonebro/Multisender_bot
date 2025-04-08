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
MIN_RECEIVER_BALANCE = 0.1
CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"
TRANSACTION_LOG = f"runtime_logs/transactions_{datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y%m%d_%H%M%S')}.log"

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

MAX_THREADS = 5
RPC_SEMAPHORE = Semaphore(MAX_THREADS)
nonce_lock = Lock()
global_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")


def get_next_nonce():
    global global_nonce
    with nonce_lock:
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
        logger.error(f"Gagal cek status transaksi nonce {nonce}: {e}")
        return "error"


def get_gas_price(multiplier=5.0, previous=None):
    try:
        gas_price = w3.eth.gas_price / 10**9 * multiplier
        if previous and gas_price <= previous:
            gas_price = previous * 1.5
        return min(gas_price, MAX_GAS_PRICE_GWEI)
    except Exception as e:
        logger.error(f"❌ Gagal ambil gas price: {e}")
        return MAX_GAS_PRICE_GWEI


def cancel_transaction(nonce, max_attempts=3):
    for attempt in range(1, max_attempts + 1):
        try:
            gas_price = get_gas_price(multiplier=20.0 + (attempt - 1) * 10.0)
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
            console.print(f"[yellow]🚫 Membatalkan nonce {nonce} (attempt {attempt}): {tx_hash.hex()[:10]}...[/yellow]")
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            return tx_hash
        except Exception as e:
            if "already known" in str(e):
                return None
            logger.error(f"Gagal membatalkan nonce {nonce} (attempt {attempt}): {e}")
            time.sleep(2)
    return None


def load_wallets(mode="random"):
    with open(CSV_FILE, newline='') as csvfile:
        reader = csv.reader(csvfile)
        all_wallets = [row[0].strip() for row in reader if row and Web3.is_address(row[0].strip())]

    if not os.path.exists(SENT_FILE):
        sent_wallets = set()
    else:
        with open(SENT_FILE) as f:
            sent_wallets = set(line.strip() for line in f)

    remaining_wallets = list(set(all_wallets) - sent_wallets)

    if mode == "random":
        random.shuffle(remaining_wallets)
    else:
        remaining_wallets.sort()

    return remaining_wallets


def filter_wallets_by_balance(wallets):
    filtered = []
    for wallet in wallets:
        try:
            balance = token_contract.functions.balanceOf(Web3.to_checksum_address(wallet)).call() / (10 ** TOKEN_DECIMALS)
            if balance >= MIN_RECEIVER_BALANCE:
                filtered.append(wallet)
        except:
            continue
    return filtered[:DAILY_WALLET_LIMIT]


def send_worker(receiver, max_retries=3):
    receiver = Web3.to_checksum_address(receiver)
    amount = round(random.uniform(MIN_TOKEN_AMOUNT, MAX_TOKEN_AMOUNT), 4)
    token_amount = int(amount * (10 ** TOKEN_DECIMALS))
    retry_delay = 3

    for attempt in range(1, max_retries + 1):
        try:
            nonce = get_next_nonce()
            base_gas = w3.eth.gas_price / 1e9
            gas_price = base_gas * (1.15 + 0.05 * (attempt - 1))
            gas_price = min(gas_price, MAX_GAS_PRICE_GWEI)

            console.print(f"[blue]🧾 TX ke {receiver} | Nonce: {nonce} | GasPrice: {gas_price:.1f} gwei[/blue]")

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
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

            if receipt.status == 1:
                msg = f"✅ Berhasil kirim {amount} token ke {receiver} | TX: {tx_hash.hex()}"
                logger.info(msg)
                console.print(msg)
                with open(SENT_FILE, "a") as f:
                    f.write(f"{receiver}\n")
                with open(TRANSACTION_LOG, "a") as logf:
                    logf.write(f"{datetime.now(pytz.timezone('Asia/Jakarta'))} | {receiver} | {amount} | {tx_hash.hex()}\n")
                return amount
            else:
                raise Exception("Transaksi gagal (status != 1)")

        except Exception as e:
            logger.error(f"❌ Attempt {attempt} gagal kirim ke {receiver}: {e}")
            console.print(f"[red]❌ Attempt {attempt} gagal kirim ke {receiver}: {e}[/red]")

            if "replacement transaction underpriced" in str(e) or "nonce too low" in str(e):
                time.sleep(retry_delay)
                continue
            elif attempt == max_retries:
                cancel_transaction(nonce)
    return 0


def show_sender_balance():
    balance = token_contract.functions.balanceOf(SENDER_ADDRESS).call() / (10 ** TOKEN_DECIMALS)
    table = Table(title="Saldo Pengirim", show_header=True, header_style="bold magenta")
    table.add_column("Alamat", style="cyan")
    table.add_column("Token", style="green")
    table.add_row(SENDER_ADDRESS, f"{balance:.4f}")
    console.print(table)
    return balance


def show_countdown_to_tomorrow():
    tz = pytz.timezone("Asia/Jakarta")
    now = datetime.now(tz)
    tomorrow = now + timedelta(days=1)
    next_run = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, tzinfo=tz)
    while True:
        remaining = next_run - datetime.now(tz)
        if remaining.total_seconds() <= 0:
            break
        console.print(f"[cyan]⌛ Countdown ke pengiriman esok hari: {remaining}[/cyan]", end="\r")
        time.sleep(1)

if __name__ == "__main__":
    console.print(Panel("[bold cyan]🚀 ERC20 Multi Sender CLI Bot[/bold cyan]", expand=False))
    show_sender_balance()

    selection_mode = Prompt.ask("Pilih metode pengambilan wallet", choices=["random", "sequential"], default="random")
    wallets = load_wallets(selection_mode)
    wallets = filter_wallets_by_balance(wallets)

    est_total = len(wallets) * ((MIN_TOKEN_AMOUNT + MAX_TOKEN_AMOUNT) / 2)
    console.print(f"[yellow]📋 Wallet valid: {len(wallets)} | Estimasi total kirim: {est_total:.2f} token[/yellow]")

    if est_total > MAX_TOTAL_SEND:
        console.print(f"[red]❌ Estimasi melebihi batas MAX_TOTAL_SEND ({MAX_TOTAL_SEND}). Kurangi jumlah wallet atau token range.[/red]")
        exit()

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(), TimeRemainingColumn(), console=console) as progress:
        task = progress.add_task("Mengirim token...", total=len(wallets))
        total_sent = 0
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = {executor.submit(send_worker, wallet): wallet for wallet in wallets}
            for future in as_completed(futures):
                amount = future.result()
                total_sent += amount
                progress.update(task, advance=1)
                if total_sent >= MAX_TOTAL_SEND:
                    console.print(f"[red]⛔ Total pengiriman mencapai limit harian: {MAX_TOTAL_SEND} token.[/red]")
                    break

    console.print(Panel(f"[green]✅ Pengiriman selesai. Total dikirim: {total_sent:.2f} token[/green]", expand=False))
    show_countdown_to_tomorrow()
