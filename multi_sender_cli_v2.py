import csv
import os
import random
import time
from datetime import datetime, time as dt_time, timedelta
import logging
import traceback
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
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler(log_path, encoding="utf-8")
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.WARNING)

formatter = JakartaFormatter(fmt="%(asctime)s [%(levelname)s] %(message)s", datefmt="[%Y-%m-%d %H:%M:%S]")
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
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_PRICE_GWEI", "200"))
EXPLORER_URL = "https://sepolia.tea.xyz/tx/"

MIN_TOKEN_AMOUNT = 10.0
MAX_TOKEN_AMOUNT = 50.0
DAILY_WALLET_LIMIT = int(os.getenv("DAILY_WALLET_LIMIT", "200"))

if not PRIVATE_KEY or not PRIVATE_KEY.startswith("0x") or len(PRIVATE_KEY) != 66:
    logger.error("❌ PRIVATE_KEY tidak valid di .env!")
    console.print("[bold red]❌ PRIVATE_KEY tidak valid di .env![/bold red]")
    exit()

if not RAW_SENDER_ADDRESS or not RPC_URL:
    logger.error("❌ SENDER_ADDRESS atau INFURA_URL tidak ditemukan di .env!")
    console.print("[bold red]❌ Konfigurasi .env tidak lengkap.[/bold red]")
    exit()

SENDER_ADDRESS = web3.Web3.to_checksum_address(RAW_SENDER_ADDRESS)

if not TOKEN_CONTRACT_RAW:
    logger.error("❌ TOKEN_CONTRACT tidak ditemukan di .env!")
    console.print("[bold red]❌ TOKEN_CONTRACT hilang.[/bold red]")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)

CSV_FILE = "wallets.csv"
SENT_FILE = "sent_wallets.txt"

logger.info(f"⚙️ Konfigurasi: MIN_TOKEN_AMOUNT={MIN_TOKEN_AMOUNT}, MAX_TOKEN_AMOUNT={MAX_TOKEN_AMOUNT}, DAILY_WALLET_LIMIT={DAILY_WALLET_LIMIT}, MAX_GAS_PRICE_GWEI={MAX_GAS_PRICE_GWEI}")

# Connect Web3
w3 = web3.Web3(web3.Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    logger.error("❌ Gagal terhubung ke jaringan! Cek RPC URL")
    console.print("[bold red]❌ Gagal terhubung ke jaringan.[/bold red]")
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

failed_addresses = []
timeout_count = 0

# Cek latensi jaringan
def check_network_latency():
    start_time = time.time()
    try:
        w3.eth.block_number
        latency = time.time() - start_time
        logger.info(f"🌐 Latensi jaringan: {latency:.2f}s")
        return latency
    except Exception as e:
        logger.error(f"❌ Gagal cek latensi jaringan: {e}")
        return float('inf')

# Cek transaksi pending di mempool
def get_pending_transactions():
    try:
        pending_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "pending")
        confirmed_nonce = w3.eth.get_transaction_count(SENDER_ADDRESS, "latest")
        if pending_nonce > confirmed_nonce:
            logger.info(f"ℹ️ Terdeteksi {pending_nonce - confirmed_nonce} transaksi pending di mempool")
            return pending_nonce
        return confirmed_nonce
    except Exception as e:
        logger.error(f"❌ Gagal cek transaksi pending: {e}")
        return w3.eth.get_transaction_count(SENDER_ADDRESS, "latest")

# Fungsi Gas Price
def get_sepolia_tea_gas_price(multiplier=2.5, previous_gas_price=None):
    url = "https://sepolia.tea.xyz/api/v1/gas-price-oracle"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        gas_price_gwei = float(data.get("fast", 0)) * multiplier
        if previous_gas_price and gas_price_gwei <= previous_gas_price:
            gas_price_gwei = previous_gas_price * 2.0
        logger.debug(f"⛽ Gas Price dari TEA: {gas_price_gwei:.2f} Gwei (multiplier: {multiplier})")
        return min(gas_price_gwei, MAX_GAS_PRICE_GWEI)
    except requests.RequestException as e:
        logger.error(f"❌ Gagal mengambil gas price dari Sepolia TEA: {e}")
        network_gas_price = w3.eth.gas_price / 10**9 * multiplier
        if previous_gas_price and network_gas_price <= previous_gas_price:
            network_gas_price = previous_gas_price * 2.0
        logger.debug(f"⛽ Gas Price dari jaringan: {network_gas_price:.2f} Gwei")
        return min(network_gas_price, MAX_GAS_PRICE_GWEI)

# Fungsi Pembatalan Transaksi
def cancel_transaction(tx_hash, nonce):
    cancel_tx = {
        'from': SENDER_ADDRESS,
        'to': SENDER_ADDRESS,
        'value': 0,
        'nonce': nonce,
        'gas': 21000,
        'gasPrice': w3.to_wei(get_sepolia_tea_gas_price(multiplier=15.0), 'gwei'),
        'chainId': w3.eth.chain_id
    }
    signed_cancel_tx = w3.eth.account.sign_transaction(cancel_tx, PRIVATE_KEY)
    try:
        cancel_hash = w3.eth.send_raw_transaction(signed_cancel_tx.raw_transaction)
        logger.info(f"🚫 Membatalkan transaksi {tx_hash} dengan {cancel_hash.hex()} | Gas Price: {w3.from_wei(cancel_tx['gasPrice'], 'gwei')} Gwei")
        w3.eth.wait_for_transaction_receipt(cancel_hash, timeout=15)
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

def log_transaction(to_address, amount, status, tx_hash_or_error, gas_used=None, confirm_time=None):
    with open(transaction_log_path, "a", encoding="utf-8") as f:
        timestamp = datetime.now(JAKARTA_TZ).strftime("%Y-%m-%d %H:%M:%S")
        gas_info = f",Gas Used: {gas_used}" if gas_used else ""
        time_info = f",Confirm Time: {confirm_time:.2f}s" if confirm_time else ""
        f.write(f"{timestamp},{to_address},{amount},{status},{tx_hash_or_error}{gas_info}{time_info}\n")

def _send_token(to_address, amount, remaining_wallets, attempt=1, previous_gas_price=None):
    global timeout_count
    from_address = SENDER_ADDRESS
    to_address = web3.Web3.to_checksum_address(to_address)
    scaled_amount = int(amount * (10 ** TOKEN_DECIMALS))

    # Cek latensi jaringan
    latency = check_network_latency()
    if latency > 5.0:
        logger.warning(f"⚠️ Latensi jaringan tinggi ({latency:.2f}s), mungkin memengaruhi konfirmasi")

    gas_multiplier = 2.5 + (attempt - 1) * 2.0  # Naik lebih agresif
    tea_gas_price = get_sepolia_tea_gas_price(multiplier=gas_multiplier, previous_gas_price=previous_gas_price)
    gas_price_to_use = min(tea_gas_price, MAX_GAS_PRICE_GWEI)

    try:
        gas_estimate = token_contract.functions.transfer(to_address, scaled_amount).estimate_gas({'from': from_address})
        gas_limit = int(gas_estimate * 1.5)
    except Exception as e:
        logger.error(f"❌ Gagal mengestimasi gas untuk {to_address} (attempt {attempt}): {str(e)}\n{traceback.format_exc()}")
        if remaining_wallets:
            new_addr, new_amt = remaining_wallets.pop(0)
            logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
            return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        raise Exception(f"Gagal estimasi gas: {e}")

    # Sinkronisasi nonce dengan cek pending
    nonce = get_pending_transactions()
    logger.debug(f"ℹ️ Nonce untuk {to_address}: Used={nonce}")

    tx = token_contract.functions.transfer(to_address, scaled_amount).build_transaction({
        'from': from_address,
        'nonce': nonce,
        'gas': gas_limit,
        'gasPrice': w3.to_wei(gas_price_to_use, 'gwei'),
        'chainId': w3.eth.chain_id
    })

    logger.debug(f"ℹ️ Tx Details (attempt {attempt}): {tx}")
    try:
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        start_time = time.time()
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        logger.info(f"📤 Transaksi dikirim ke {to_address}: {tx_hash.hex()} | Gas Price: {gas_price_to_use} Gwei | Gas Limit: {gas_limit} | Nonce: {nonce}")

        tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        confirm_time = time.time() - start_time
        if tx_receipt.status != 1:
            logger.error(f"❌ Transaksi {tx_hash.hex()} gagal di chain: Status {tx_receipt.status}\nReceipt: {tx_receipt}")
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception(f"Transaksi gagal: Status {tx_receipt.status}")
        logger.info(f"✅ Transaksi {tx_hash.hex()} dikonfirmasi | Gas Used: {tx_receipt.gasUsed} | Confirm Time: {confirm_time:.2f}s")
        timeout_count = 0
        return tx_hash.hex(), tx_receipt.gasUsed, confirm_time
    except web3.exceptions.TimeExhausted:
        logger.error(f"⏰ Transaksi {tx_hash.hex()} timeout setelah 30 detik (attempt {attempt})\n{traceback.format_exc()}")
        timeout_count += 1
        cancel_hash = cancel_transaction(tx_hash.hex(), nonce)
        if cancel_hash:
            logger.info(f"✅ Transaksi lama dibatalkan dengan {cancel_hash.hex()}, lanjut ke alamat berikutnya")
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        else:
            logger.error(f"❌ Gagal membatalkan transaksi {tx_hash.hex()}, sinkronisasi nonce")
            time.sleep(1)
            updated_nonce = get_pending_transactions()
            logger.info(f"ℹ️ Nonce diperbarui setelah timeout: {updated_nonce}")
            if updated_nonce > nonce:
                logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        if timeout_count >= 2:
            logger.warning(f"⚠️ Timeout berturut-turut ({timeout_count}x), skip {to_address} ke alamat berikutnya")
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception("Timeout berturut-turut dan tidak ada alamat pengganti")
        if attempt < 3:
            logger.info(f"🔄 Mencoba lagi dengan gas lebih tinggi untuk {to_address} (attempt {attempt + 1})")
            return _send_token(to_address, amount, remaining_wallets, attempt + 1, previous_gas_price=gas_price_to_use)
        logger.warning(f"⚠️ Maksimum attempt tercapai untuk {to_address}, lanjut ke alamat berikutnya")
        if remaining_wallets:
            new_addr, new_amt = remaining_wallets.pop(0)
            logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
            return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        raise Exception("Timeout setelah 3 attempt dan tidak ada alamat pengganti")
    except web3.exceptions.Web3RPCError as e:
        error_msg = str(e)
        if "already known" in error_msg:
            logger.error(f"❌ Transaksi {tx_hash.hex()} sudah ada di mempool (attempt {attempt}): {error_msg}")
            time.sleep(1)
            updated_nonce = get_pending_transactions()
            logger.info(f"ℹ️ Nonce diperbarui setelah 'already known': {updated_nonce}")
            if updated_nonce > nonce:
                logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            if attempt < 3:
                logger.info(f"🔄 Mencoba lagi dengan gas lebih tinggi untuk {to_address} (attempt {attempt + 1})")
                return _send_token(to_address, amount, remaining_wallets, attempt + 1, previous_gas_price=gas_price_to_use)
            logger.warning(f"⚠️ Maksimum attempt tercapai untuk {to_address}, lanjut ke alamat berikutnya")
            if remaining_wallets:
                new_addr, new_amt = remaining_wallets.pop(0)
                logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception("'Already known' setelah 3 attempt dan tidak ada alamat pengganti")
        elif "replacement transaction underpriced" in error_msg:
            logger.error(f"❌ Transaksi {tx_hash.hex()} underpriced (attempt {attempt}): {error_msg}")
            cancel_hash = cancel_transaction(tx_hash.hex(), nonce)
            if cancel_hash:
                logger.info(f"✅ Transaksi lama dibatalkan dengan {cancel_hash.hex()}, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            else:
                logger.error(f"❌ Gagal membatalkan transaksi {tx_hash.hex()}, tingkatkan gas dan retry")
                time.sleep(1)
                updated_nonce = get_pending_transactions()
                logger.info(f"ℹ️ Nonce diperbarui setelah underpriced: {updated_nonce}")
                if updated_nonce > nonce:
                    logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi, lanjut ke alamat berikutnya")
                    if remaining_wallets:
                        new_addr, new_amt = remaining_wallets.pop(0)
                        logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                        return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
                if attempt < 3:
                    logger.info(f"🔄 Mencoba lagi dengan gas lebih tinggi untuk {to_address} (attempt {attempt + 1})")
                    return _send_token(to_address, amount, remaining_wallets, attempt + 1, previous_gas_price=gas_price_to_use)
                logger.warning(f"⚠️ Maksimum attempt tercapai untuk {to_address}, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
                raise Exception("Underpriced setelah 3 attempt dan tidak ada alamat pengganti")
        raise
    except ValueError as e:
        logger.error(f"❌ ValueError saat mengirim transaksi ke {to_address}: {str(e)}\n{traceback.format_exc()}")
        if "replacement transaction underpriced" in str(e) or "future transaction tries to replace pending" in str(e):
            logger.error(f"❌ Transaksi {tx_hash.hex()} underpriced atau konflik nonce (attempt {attempt})")
            cancel_hash = cancel_transaction(tx_hash.hex(), nonce)
            if cancel_hash:
                logger.info(f"✅ Transaksi lama dibatalkan dengan {cancel_hash.hex()}, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            else:
                logger.error(f"❌ Gagal membatalkan transaksi {tx_hash.hex()}, sinkronisasi nonce")
                time.sleep(1)
                updated_nonce = get_pending_transactions()
                logger.info(f"ℹ️ Nonce diperbarui setelah underpriced: {updated_nonce}")
                if updated_nonce > nonce:
                    logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi")
                    if remaining_wallets:
                        new_addr, new_amt = remaining_wallets.pop(0)
                        logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                        return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
                if attempt < 3:
                    logger.info(f"🔄 Mencoba lagi dengan gas lebih tinggi untuk {to_address} (attempt {attempt + 1})")
                    return _send_token(to_address, amount, remaining_wallets, attempt + 1, previous_gas_price=gas_price_to_use)
                logger.warning(f"⚠️ Maksimum attempt tercapai untuk {to_address}, lanjut ke alamat berikutnya")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
            raise Exception("Underpriced atau konflik nonce dan gagal membatalkan transaksi")
        elif "nonce too low" in str(e):
            logger.error(f"❌ Nonce terlalu rendah untuk {tx_hash.hex()}, sinkronisasi ulang")
            time.sleep(1)
            updated_nonce = get_pending_transactions()
            logger.info(f"ℹ️ Nonce diperbarui ke: {updated_nonce}")
            return _send_token(to_address, amount, remaining_wallets, attempt=1)
        elif "transaction already imported" in str(e):
            logger.error(f"❌ Transaksi {tx_hash.hex()} sudah ada di mempool, sinkronisasi nonce")
            time.sleep(1)
            updated_nonce = get_pending_transactions()
            if updated_nonce > nonce:
                logger.info(f"✅ Transaksi lama di-drop atau dikonfirmasi")
                if remaining_wallets:
                    new_addr, new_amt = remaining_wallets.pop(0)
                    logger.info(f"🔄 Ganti dengan alamat baru: {new_addr}")
                    return _send_token(new_addr, new_amt, remaining_wallets, attempt=1)
        raise

def send_token(to_address, amount, remaining_wallets):
    try:
        tx_hash, gas_used, confirm_time = _send_token(to_address, amount, remaining_wallets)
        logger.info(f"✅ Token terkirim ke {to_address} | Amount: {amount:.4f} | TxHash: {tx_hash} | Gas Used: {gas_used} | Confirm Time: {confirm_time:.2f}s")
        log_transaction(to_address, amount, "SUCCESS", tx_hash, gas_used, confirm_time)
        with open(SENT_FILE, "a") as f:
            f.write(f"{to_address}\n")
        return True, amount
    except Exception as e:
        error_detail = f"{str(e)}\n{traceback.format_exc()}"
        logger.error(f"❌ Gagal mengirim ke {to_address}: {error_detail}")
        log_transaction(to_address, amount, "FAILED", error_detail)
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

    logger.info(f"✅ Pengiriman selesai. Total wallet dikirim: {total_wallets_sent}, Total token: {total_token_sent:.4f}")
    console.print(f"[bold green]📦 Total wallet berhasil dikirim: {total_wallets_sent} | Total token: {total_token_sent:.4f}[/bold green]")
    return total_wallets_sent

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
    return True

if __name__ == "__main__":
    main()
