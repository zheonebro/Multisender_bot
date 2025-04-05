import csv
import os
import random
import time
from datetime import datetime
import traceback
import threading

from dotenv import load_dotenv
from rich import print
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.align import Align
import web3
import schedule

# Init
console = Console()
load_dotenv()

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RAW_SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
SENDER_ADDRESS = web3.Web3.to_checksum_address(RAW_SENDER_ADDRESS)
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_RAW = os.getenv("TOKEN_CONTRACT")
EXPLORER_URL = "https://sepolia.tea.xyz/"

if not TOKEN_CONTRACT_RAW:
    console.print("[bold red]❌ Environment variable 'TOKEN_CONTRACT' tidak ditemukan atau kosong![/bold red]")
    exit()

TOKEN_CONTRACT_ADDRESS = web3.Web3.to_checksum_address(TOKEN_CONTRACT_RAW)
CSV_FILE = "wallets_checksummed.csv"

# Connect Web3
w3 = web3.Web3(web3.Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    console.print("[bold red]❌ Gagal terhubung ke jaringan! Cek RPC URL[/bold red]")
    exit()

# ERC20 ABI
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
]

# Contract
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
decimals = token_contract.functions.decimals().call()

nonce_lock = threading.Lock()

def convert_addresses_to_checksum(input_file, output_file):
    try:
        with open(input_file, newline='') as infile, open(output_file, 'w', newline='') as outfile:
            reader = csv.DictReader(infile)
            writer = csv.DictWriter(outfile, fieldnames=['address'])
            writer.writeheader()
            for row in reader:
                try:
                    checksummed = web3.Web3.to_checksum_address(row['address'].strip())
                    writer.writerow({'address': checksummed})
                except:
                    continue
    except Exception as e:
        console.print(f"[red]Gagal konversi checksum: {e}[/red]")

def load_wallets(csv_file):
    valid_addresses = []
    try:
        with open(csv_file, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    address = web3.Web3.to_checksum_address(row['address'].strip())
                    valid_addresses.append(address)
                except:
                    continue
    except Exception as e:
        console.print(f"[red]Gagal membaca file: {e}[/red]")
    return valid_addresses

def get_token_balance(address):
    return token_contract.functions.balanceOf(address).call() / (10 ** decimals)

def get_eth_balance(address):
    return w3.eth.get_balance(address) / 10**18

def send_token(to_address, amount):
    try:
        to_address = web3.Web3.to_checksum_address(to_address)
    except:
        raise ValueError("Alamat tidak valid")

    with nonce_lock:
        nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)

    tx_func = token_contract.functions.transfer(to_address, int(amount * (10 ** decimals)))
    gas_estimate = tx_func.estimate_gas({'from': SENDER_ADDRESS})
    gas_price = w3.eth.gas_price

    tx = tx_func.build_transaction({
        'chainId': w3.eth.chain_id,
        'gas': gas_estimate,
        'gasPrice': gas_price,
        'nonce': nonce
    })

    try:
        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        if not hasattr(signed_tx, "rawTransaction"):
            raise AttributeError("SignedTransaction object missing 'rawTransaction'")
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        return w3.to_hex(tx_hash)
    except Exception as e:
        raise Exception(f"Gagal menandatangani atau mengirim transaksi: {e}")

def send_tokens(csv_file, min_amt, max_amt, count=None):
    addresses = load_wallets(csv_file)
    if count:
        addresses = random.sample(addresses, min(count, len(addresses)))

    log_lines = []
    for i, address in enumerate(addresses, start=1):
        try:
            amount = round(random.uniform(min_amt, max_amt), 6)
            tx_hash = send_token(address, amount)
            tx_short = tx_hash[:10]
            link = f"[link={EXPLORER_URL}{tx_hash}]{tx_short}...[/link]"
            token_bal = get_token_balance(SENDER_ADDRESS)
            eth_bal = get_eth_balance(SENDER_ADDRESS)
            log = f"{i}. [green]{address}[/green] ✅ {amount:.4f} | Token: [bold]{token_bal:.4f}[/bold] | TEA: [bold]{eth_bal:.4f}[/bold] | TX: {link}"
            console.print(log)
            log_lines.append(log)
            time.sleep(random.uniform(0.5, 1.2))
        except Exception as e:
            log = f"{i}. [red]{address}[/red] ❌ ERROR: {str(e)}"
            console.print(log)
            log_lines.append(log)

    panel = Panel("\n".join(log_lines), title="📬 Ringkasan Pengiriman", border_style="bright_blue")
    sisa_token = get_token_balance(SENDER_ADDRESS)
    sisa_eth = get_eth_balance(SENDER_ADDRESS)
    console.print(panel)
    console.print(f"[green]✅ Sisa Token: {sisa_token:.4f} | Sisa TEA: {sisa_eth:.4f}[/green]")

def schedule_job(csv_file, min_amt, max_amt, schedule_time):
    def job():
        console.print("[bold magenta]\n🚀 Mengirim token ke 150 address acak...[/bold magenta]")
        send_tokens(csv_file, min_amt, max_amt, count=150)

    console.print("[yellow]⏳ Menunggu waktu terjadwal...[/yellow]")
    schedule.every().day.at(schedule_time).do(job)

    while True:
        schedule.run_pending()
        time.sleep(5)

def main():
    banner = Align.center("""
███╗   ███╗██╗   ██╗██╗██╗     ███████╗███████╗██████╗ 
████╗ ████║██║   ██║██║██║     ██╔════╝██╔════╝██╔══██╗
██╔████╔██║██║   ██║██║██║     █████╗  █████╗  ██║  ██║
██║╚██╔╝██║██║   ██║██║██║     ██╔══╝  ██╔══╝  ██║  ██║
██║ ╚═╝ ██║╚██████╔╝██║███████╗███████╗███████╗██████╔╝
╚═╝     ╚═╝ ╚═════╝ ╚═╝╚══════╝╚══════╝╚══════╝╚═════╝ 
""", vertical="middle")
    console.print(banner, style="bold blue")

    convert_addresses_to_checksum("wallets.csv", CSV_FILE)

    min_amt = float(Prompt.ask("🔢 Jumlah MIN token", default="5"))
    max_amt = float(Prompt.ask("🔢 Jumlah MAX token", default="20"))

    console.print("\n[bold green]Pilih mode pengiriman:[/bold green]")
    console.print("[cyan][1][/cyan] Kirim Sekali (langsung)")
    console.print("[cyan][2][/cyan] Kirim Sekarang + Terjadwal")
    console.print("[cyan][3][/cyan] Hanya Terjadwal (uji 10 address dulu)")

    mode = Prompt.ask("📌 Pilihan Anda", choices=["1", "2", "3"], default="1")

    if mode == "1":
        send_tokens(CSV_FILE, min_amt, max_amt)
    elif mode == "2":
        schedule_time = Prompt.ask("⏰ Jadwal harian berikutnya (HH:MM)", default="09:00")
        send_tokens(CSV_FILE, min_amt, max_amt)
        schedule_job(CSV_FILE, min_amt, max_amt, schedule_time)
    elif mode == "3":
        console.print("[bold yellow]🔍 Pengujian: Kirim ke 10 address acak...[/bold yellow]")
        send_tokens(CSV_FILE, min_amt, max_amt, count=10)
        schedule_time = Prompt.ask("⏰ Jadwal pengiriman (HH:MM)", default="09:00")
        schedule_job(CSV_FILE, min_amt, max_amt, schedule_time)

if __name__ == "__main__":
    main()
