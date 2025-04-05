import csv
import random
import time
import schedule
from web3 import Web3
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
SENDER_ADDRESS = os.getenv("SENDER_ADDRESS")
RPC_URL = os.getenv("INFURA_URL")

# Token contract address (fixed)
TOKEN_CONTRACT_ADDRESS = Web3.to_checksum_address("0xbB5b70Ac7e8CE2cA9afa044638CBb545713eC34F")

# Token ABI (ERC20 basic)
ERC20_ABI = '''
[
    {"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
]
'''

# Init Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    print("❌ Gagal terhubung ke RPC.")
    exit()

# Contract instance
token_contract = w3.eth.contract(address=TOKEN_CONTRACT_ADDRESS, abi=ERC20_ABI)
decimals = token_contract.functions.decimals().call()

def load_addresses(filename):
    with open(filename, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        return [row['address'] for row in reader]

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

def send_tokens_random(csv_file, min_amount, max_amount):
    addresses = load_addresses(csv_file)
    for address in addresses:
        for attempt in range(3):
            try:
                amount = round(random.uniform(min_amount, max_amount), 6)
                print(f"🔄 Mengirim {amount} token ke {address}...")
                tx_hash = send_token(address, amount)
                print(f"✅ Sukses kirim: {tx_hash}")
                break
            except Exception as e:
                print(f"❌ Gagal kirim ke {address} (percobaan {attempt+1}): {e}")
                time.sleep(3)
        else:
            print(f"⚠️ Gagal permanen kirim ke {address}")

def run_scheduled(csv_file, min_amount, max_amount):
    def job():
        print(f"\n🕒 Menjalankan pengiriman token ({time.strftime('%Y-%m-%d %H:%M:%S')})")
        send_tokens_random(csv_file, min_amount, max_amount)

    schedule.every().day.at("09:00").do(job)  # Waktu pengiriman disetel pukul 09:00 server
    print("📅 Bot dijadwalkan setiap hari jam 09:00")
    while True:
        schedule.run_pending()
        time.sleep(10)

def main():
    print("=== BOT MULTISENDER TERJADWAL ===")
    csv_file = input("Masukkan nama file CSV (misal: wallets.csv): ").strip()
    min_amount = float(input("Jumlah MIN token yang dikirim: "))
    max_amount = float(input("Jumlah MAX token yang dikirim: "))

    run_scheduled(csv_file, min_amount, max_amount)

if __name__ == "__main__":
    main()
