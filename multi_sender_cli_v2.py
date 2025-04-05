import csv
import os
import random
import time
from datetime import datetime

from dotenv import load_dotenv
from rich import print
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.align import Align
from web3 import Web3
import schedule

# Init
console = Console()
load_dotenv()

# Config
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")
TOKEN_CONTRACT_ADDRESS = Web3.to_checksum_address("0xbB5b70Ac7e8CE2cA9afa044638CBb545713eC34F")
CSV_FILE = "wallets.csv"

# Connect Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    console.print("[bold red]❌ Gagal terhubung ke jaringan! Cek RPC URL[/bold red]")
    exit()

# ERC20 ABI
ERC20_ABI = '''
[
    {"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
]
'''

token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
decimals = token_contract.functions.decimals().call()

def convert_addresses_to_checksum(input_file, output_file):
    try:
        with open(input_file, newline='') as infile, open(output_file, 'w', newline='') as outfile:
            reader = csv.DictReader(infile)
            fieldnames = ['address']
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()

            for row in reader:
                try:
                    checksummed = Web3.to_checksum_address(row['address'].strip())
                    writer.writerow({'address': checksummed})
                except Exception:
                    console.print(f"[yellow]⚠️ Alamat dilewati karena tidak valid: {row['address']}[/yellow]")
        console.print("[green]✅ File berhasil dikonversi ke checksum: wallets_checksummed.csv[/green]")
    except Exception as e:
        console.print(f"[red]Gagal konversi checksum: {e}[/red]")

def load_wallets(csv_file):
    valid_addresses = []
    try:
        with open(csv_file, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                address = row['address'].strip()
                try:
                    checksummed = Web3.to_checksum_address(address)
                    valid_addresses.append(checksummed)
                except Exception:
                    console.print(f"[yellow]⚠️ Alamat tidak valid dilewati: {address}[/yellow]")
    except Exception as e:
        console.print(f"[red]Gagal membaca file: {e}[/red]")
    return valid_addresses

def send_token(to_address, amount):
    try:
        to_address = Web3.to_checksum_address(to_address)
    except Exception as e:
        raise ValueError(f"Alamat tidak valid: {to_address}") from e

    nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
    tx = token_contract.functions.transfer(
        to_address,
        int(amount * (10 ** decimals))
    ).build_transaction({
        'chainId': w3.eth.chain_id,
        'gas': 100000,
        'gasPrice': w3.to_wei(2.4, 'gwei'),  # Set gas price secara manual
        'nonce': nonce
    })
    signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    return w3.to_hex(tx_hash)

def send_tokens(csv_file, min_amount, max_amount):
    addresses = load_wallets(csv_file)
    if not addresses:
        return

    table = Table(title="📤 Status Pengiriman", show_lines=True)
    table.add_column("No", justify="center", style="bold")
    table.add_column("Address", justify="left", style="cyan")
    table.add_column("Jumlah", justify="right", style="magenta")
    table.add_column("Status", justify="center")

    with Progress(
        SpinnerColumn(),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console
    ) as progress:
        task = progress.add_task("Mengirim token...", total=len(addresses))

        for i, address in enumerate(addresses, start=1):
            for attempt in range(3):
                try:
                    amount = round(random.uniform(min_amount, max_amount), 6)
                    tx_hash = send_token(address, amount)
                    log_message = f"[green][{datetime.now()}] ✅ Sukses kirim {amount} token ke {address} | TX: {tx_hash}[/green]"
                    console.print(log_message)
                    table.add_row(str(i), address, str(amount), f"✅ {tx_hash[:10]}...")
                    break
                except Exception as e:
                    if attempt == 2:
                        log_message = f"[red][{datetime.now()}] ❌ Gagal kirim ke {address}: {e}[/red]"
                        console.print(log_message)
                        table.add_row(str(i), address, "-", f"❌ {e}")
                    else:
                        time.sleep(2)
            progress.update(task, advance=1)

    console.print(Panel(table, title="📬 Ringkasan Pengiriman", border_style="bright_blue"))

def schedule_job(csv_file, min_amt, max_amt, schedule_time):
    def job():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        console.print(f"\n[cyan]⏰ [{now}] Menjalankan pengiriman token terjadwal...[/cyan]")
        send_tokens(csv_file, min_amt, max_amt)

    console.print("[bold magenta]\n🚀 Pengiriman awal dimulai sekarang...[/bold magenta]")
    send_tokens(csv_file, min_amt, max_amt)

    schedule.every().day.at(schedule_time).do(job)
    console.print(f"[bold green]✅ Bot dijadwalkan setiap hari jam {schedule_time}[/bold green]")

    while True:
        schedule.run_pending()
        time.sleep(10)

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

    console.print("[bold cyan]=== BOT MULTISENDER ERC20 TERJADWAL ===[/bold cyan]", justify="center")
    convert_addresses_to_checksum(CSV_FILE, "wallets_checksummed.csv")

    min_amt = float(Prompt.ask("🔢 Jumlah MIN token", default="5"))
    max_amt = float(Prompt.ask("🔢 Jumlah MAX token", default="20"))

    console.print(f"\n[blue]📦 Token dari: [white]{SENDER_ADDRESS}[/white][/blue]")
    console.print(f"[blue]📁 CSV Target: [white]{CSV_FILE}[/white][/blue]")
    console.print(f"[blue]🎯 Rentang Token: [white]{min_amt} - {max_amt}[/white][/blue]\n")

    schedule_time = Prompt.ask("⏰ Masukkan waktu pengiriman harian berikutnya (format 24 jam HH:MM)", default="09:00")
    schedule_job("wallets_checksummed.csv", min_amt, max_amt, schedule_time)

if __name__ == "__main__":
    main()
