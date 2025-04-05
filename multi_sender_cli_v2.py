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

def load_wallets(csv_file):
    try:
        with open(csv_file, newline='') as f:
            reader = csv.DictReader(f)
            return [row['address'] for row in reader]
    except Exception as e:
        console.print(f"[red]Gagal membaca file: {e}[/red]")
        return []

def send_token(to_address, amount):
    nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
    tx = token_contract.functions.transfer(
        Web3.to_checksum_address(to_address),
        int(amount * (10 ** decimals))
    ).build_transaction({
        'chainId': w3.eth.chain_id,
        'gas': 100000,
        'gasPrice': w3.eth.gas_price,
        'nonce': nonce
    })
    signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    return w3.to_hex(tx_hash)

def send_tokens(csv_file, min_amount, max_amount):
    addresses = load_wallets(csv_file)
    if not addresses:
        return

    table = Table(title="📤 Status Pengiriman")
    table.add_column("No", justify="right")
    table.add_column("Address", justify="left")
    table.add_column("Jumlah", justify="right")
    table.add_column("Status", justify="left")

    for i, address in enumerate(addresses, start=1):
        for attempt in range(3):
            try:
                amount = round(random.uniform(min_amount, max_amount), 6)
                tx_hash = send_token(address, amount)
                table.add_row(str(i), address, str(amount), f"[green]✅ Sukses: {tx_hash[:10]}...[/green]")
                break
            except Exception as e:
                if attempt == 2:
                    table.add_row(str(i), address, "-", f"[red]❌ Gagal: {e}[/red]")
                else:
                    time.sleep(2)
    console.print(table)

def schedule_job(csv_file, min_amt, max_amt):
    def job():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        console.print(f"\n[cyan]⏰ [{now}] Menjalankan pengiriman token terjadwal...[/cyan]")
        send_tokens(csv_file, min_amt, max_amt)

    schedule.every().day.at("09:00").do(job)
    console.print("[bold green]✅ Bot dijadwalkan setiap hari jam 09:00[/bold green]")

    while True:
        schedule.run_pending()
        time.sleep(10)

def main():
    console.print("[bold cyan]=== BOT MULTISENDER ERC20 TERJADWAL ===[/bold cyan]")
    csv_file = Prompt.ask("📂 Masukkan nama file CSV", default="wallets.csv")
    min_amt = float(Prompt.ask("🔢 Jumlah MIN token", default="5"))
    max_amt = float(Prompt.ask("🔢 Jumlah MAX token", default="20"))

    console.print(f"\n[blue]📦 Token dari: [white]{SENDER_ADDRESS}[/white][/blue]")
    console.print(f"[blue]📁 CSV Target: [white]{csv_file}[/white][/blue]")
    console.print(f"[blue]🎯 Rentang Token: [white]{min_amt} - {max_amt}[/white][/blue]\n")

    confirm = Prompt.ask("▶️ Mulai pengiriman terjadwal? (y/n)", choices=["y", "n"], default="y")
    if confirm == "y":
        schedule_job(csv_file, min_amt, max_amt)

if __name__ == "__main__":
    main()
