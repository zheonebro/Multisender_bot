import os
import random
import time
import logging
from datetime import datetime
import pytz
from dotenv import load_dotenv
from web3 import Web3
import csv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.prompt import IntPrompt

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
SENDER_ADDRESS = Web3.to_checksum_address(os.getenv("SENDER_ADDRESS"))
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("TOKEN_CONTRACT"))
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "1000"))

MIN_TOKEN_AMOUNT = 10.0
MAX_TOKEN_AMOUNT = 50.0
DAILY_WALLET_LIMIT = int(os.getenv("DAILY_WALLET_LIMIT", "200"))
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
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}
]
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=TOKEN_ABI)
TOKEN_DECIMALS = token_contract.functions.decimals().call()

def get_gas_price(multiplier=5.0, previous=None):
    try:
        gas_price = w3.eth.gas_price / 10**9 * multiplier
        if previous and gas_price <= previous:
            gas_price = previous * 1.5
        return min(gas_price, MAX_GAS_PRICE_GWEI)
    except Exception as e:
        logger.error(f"❌ Gagal ambil gas price: {e}")
        return MAX_GAS_PRICE_GWEI

def cancel_transaction(nonce):
    try:
        high_gas_price = get_gas_price(multiplier=20.0)
        tx = {
            'from': SENDER_ADDRESS,
            'to': SENDER_ADDRESS,
            'value': 0,
            'nonce': nonce,
            'gas': 21000,
            'gasPrice': w3.to_wei(high_gas_price, 'gwei'),
            'chainId': w3.eth.chain_id
        }
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        console.print(f"[yellow]🚫 Membatalkan nonce {nonce} dengan gas {high_gas_price:.1f} Gwei: [bold]{tx_hash.hex()[:8]}...[/bold][/yellow]")
        logger.info(f"🚫 Membatalkan nonce {nonce} dengan gas {high_gas_price:.1f} Gwei: {tx_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        console.print(f"[green]✅ Nonce {nonce} berhasil dibatalkan: {tx_hash.hex()[:8]}...[/green]")
        return tx_hash
    except Exception as e:
        logger.error(f"❌ Gagal membatalkan nonce {nonce}: {e}")
        console.print(f"[red]❌ Gagal membatalkan nonce {nonce}: {e}[/red]")
        return None

def cancel_pending_transactions(start_nonce, end_nonce):
    """Batalkan rentang nonce tertentu."""
    with Progress(
        TextColumn("[bold yellow]Membatalkan transaksi pending..."),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn()
    ) as progress:
        task = progress.add_task("", total=end_nonce - start_nonce + 1)
        for nonce in range(start_nonce, end_nonce + 1):
            cancel_transaction(nonce)
            progress.advance(task)

def _send_token(to_address, amount, max_attempts=3):
    to_address = Web3.to_checksum_address(to_address)
    scaled_amount = int(amount * 10**TOKEN_DECIMALS)
    pending_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
    latest_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "latest")
    nonce = max(pending_nonce, latest_nonce)
    logger.info(f"ℹ️ Nonce untuk {to_address[:8]}...: Pending={pending_nonce}, Latest={latest_nonce}, Used={nonce}")

    timeout_count = 0
    for attempt in range(1, max_attempts + 1):
        gas_price = get_gas_price(multiplier=5.0 + (attempt - 1) * 5.0)
        try:
            gas_limit = int(token_contract.functions.transfer(to_address, scaled_amount).estimate_gas({'from': SENDER_ADDRESS}) * 2.0)
            tx = token_contract.functions.transfer(to_address, scaled_amount).build_transaction({
                'from': SENDER_ADDRESS, 'nonce': nonce, 'gas': gas_limit, 'gasPrice': w3.to_wei(gas_price, 'gwei'), 'chainId': w3.eth.chain_id
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            start_time = time.time()
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            console.print(f"[cyan]📤 Mengirim ke {to_address[:8]}...: [bold]{tx_hash.hex()[:8]}...[/bold] (Gas: {gas_price:.1f} Gwei, Attempt {attempt})")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                confirm_time = time.time() - start_time
                timeout_count = 0
                return tx_hash.hex(), receipt.gasUsed, confirm_time
            logger.error(f"❌ Transaksi {tx_hash.hex()} gagal: Status {receipt.status}")
            return None, 0, 0
        except Exception as e:
            error_msg = str(e)
            if "replacement transaction underpriced" in error_msg:
                console.print(f"[yellow]⚠️ Underpriced ke {to_address[:8]}... (attempt {attempt})[/yellow]")
                cancel_transaction(nonce)
            elif "TimeExhausted" in error_msg:
                console.print(f"[yellow]⏰ Timeout ke {to_address[:8]}... (attempt {attempt})[/yellow]")
                timeout_count += 1
                cancel_hash = cancel_transaction(nonce)
                if cancel_hash:
                    nonce += 1
                if timeout_count >= 2:
                    console.print(f"[red]❌ Timeout berturut-turut ({timeout_count}x) untuk {to_address[:8]}...[/red]")
                    return None, 0, 0
            elif "already known" in error_msg:
                console.print(f"[yellow]⚠️ Transaksi sudah ada di mempool untuk {to_address[:8]}... (attempt {attempt})[/yellow]")
                cancel_transaction(nonce)
                nonce += 1
            else:
                console.print(f"[red]❌ Error ke {to_address[:8]}...: {error_msg[:50]}...[/red]")
            if attempt == max_attempts:
                console.print(f"[red]❌ Maksimum attempt tercapai untuk {to_address[:8]}...[/red]")
                return None, 0, 0
            time.sleep(2)

def send_token(to_address, amount):
    tx_hash, gas_used, confirm_time = _send_token(to_address, amount)
    status = "SUCCESS" if tx_hash else "FAILED"
    with open(TRANSACTION_LOG, "a") as f:
        f.write(f"{datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S')},{to_address},{amount},{status},{tx_hash or 'Gagal'},{gas_used},{confirm_time:.2f}\n")
    if tx_hash:
        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address}\n")
        console.print(f"[green]✅ Terkirim ke {to_address[:8]}...: {tx_hash[:8]}... ({confirm_time:.2f}s)[/green]")
        return True, amount
    console.print(f"[red]❌ Gagal mengirim ke {to_address[:8]}...[/red]")
    return False, 0

def load_random_wallets(limit=DAILY_WALLET_LIMIT):
    wallets = []
    try:
        with open(CSV_FILE, "r") as f:
            reader = csv.reader(f)
            for row in reader:
                if row and w3.is_address(row[0]):
                    addr = w3.to_checksum_address(row[0])
                    wallets.append(addr)
        if not wallets:
            raise ValueError("Tidak ada alamat valid di wallets.csv")
        random.shuffle(wallets)
        selected_wallets = wallets[:limit]
        return [(addr, random.uniform(MIN_TOKEN_AMOUNT, MAX_TOKEN_AMOUNT)) for addr in selected_wallets]
    except Exception as e:
        logger.error(f"❌ Gagal memuat wallets.csv: {e}")
        console.print(f"[red]❌ Gagal memuat wallets.csv: {e}[/red]")
        return []

def check_pending_transactions():
    """Periksa jumlah transaksi pending dan kembalikan jumlahnya."""
    pending_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
    latest_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "latest")
    pending_count = pending_nonce - latest_nonce
    return pending_count, latest_nonce, pending_nonce

def main():
    console.print(Panel("🚀 TEA Sepolia Sender Bot", style="bold cyan", border_style="green"))
    os.makedirs("runtime_logs", exist_ok=True)

    # Periksa transaksi pending
    pending_count, latest_nonce, pending_nonce = check_pending_transactions()
    console.print(f"[blue]ℹ️ Status Nonce: Pending={pending_nonce}, Latest={latest_nonce}[/blue]")
    
    if pending_count > 0:
        console.print(f"[yellow]⚠️ Terdeteksi {pending_count} transaksi pending di mempool[/yellow]")
        choice = IntPrompt.ask("[yellow]⚙️ Batalkan semua transaksi pending? (1 = Ya, 0 = Tidak)[/yellow]", default=0, choices=["0", "1"])
        if choice == 1:
            console.print(f"[blue]ℹ️ Membatalkan {pending_count} transaksi pending (nonce {latest_nonce} hingga {pending_nonce - 1})[/blue]")
            cancel_pending_transactions(latest_nonce, pending_nonce - 1)
            console.print("[green]✅ Semua transaksi pending telah dibatalkan[/green]")
        else:
            console.print("[blue]ℹ️ Melanjutkan pengiriman dengan transaksi pending[/blue]")
    else:
        console.print("[green]✅ Tidak ada transaksi pending di mempool[/green]")

    if os.path.exists(SENT_FILE):
        os.remove(SENT_FILE)
    
    wallets = load_random_wallets()
    if not wallets:
        console.print("[yellow]🚫 Tidak ada wallet valid untuk diproses.[/yellow]")
        return

    console.print(f"[blue]ℹ️ Mengirim ke {len(wallets)} alamat acak dari wallets.csv[/blue]")
    total_sent, total_tokens = 0, 0.0
    with Progress(
        TextColumn("[bold green]Mengirim token..."),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeRemainingColumn()
    ) as progress:
        task = progress.add_task("", total=len(wallets))
        for addr, amt in wallets:
            success, sent_amount = send_token(addr, amt)
            if success:
                total_sent += 1
                total_tokens += sent_amount
            progress.advance(task)

    console.print(Panel(f"✅ Selesai: {total_sent} wallet | {total_tokens:.2f} token", style="bold green"))

if __name__ == "__main__":
    main()
