import csv
import os
import time
import random
from dotenv import load_dotenv
from web3 import Web3
import schedule
from datetime import datetime

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
RPC_URL = os.getenv("INFURA_URL")
SENDER_ADDRESS = Web3.to_checksum_address(os.getenv("SENDER_ADDRESS"))

w3 = Web3(Web3.HTTPProvider(RPC_URL))
if not w3.is_connected():
    raise Exception("❌ Gagal terhubung ke RPC!")

erc20_abi = [
    {
        "constant": False,
        "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}],
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

def check_balance(token, decimals):
    raw_balance = token.functions.balanceOf(SENDER_ADDRESS).call()
    return raw_balance / (10 ** decimals)

def send_token(token, to_address, amount, decimals, max_retries=3, delay=5):
    to_address = Web3.to_checksum_address(to_address)
    retries = 0
    while retries < max_retries:
        try:
            nonce = w3.eth.get_transaction_count(SENDER_ADDRESS)
            amount_wei = int(amount * (10 ** decimals))
            tx = token.functions.transfer(to_address, amount_wei).build_transaction({
                'from': SENDER_ADDRESS,
                'nonce': nonce,
                'gas': 100000,
                'gasPrice': w3.to_wei('5', 'gwei'),
            })
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            print(f"✅ {amount} tokens sent to {to_address}. TxHash: {w3.to_hex(tx_hash)}")
            return True
        except Exception as e:
            retries += 1
            print(f"⚠️ Gagal kirim ke {to_address}, percobaan {retries}/{max_retries}: {e}")
            time.sleep(delay)
    print(f"❌ Gagal kirim ke {to_address} setelah {max_retries} kali.")
    return False

def process_csv(file_path, token_address, mode_random=False, min_amount=0, max_amount=0):
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=erc20_abi)
    decimals = token.functions.decimals().call()
    balance = check_balance(token, decimals)
    failed_addresses = []
    total_to_send = 0

    with open(file_path, "r") as file:
        reader = csv.DictReader(file)
        rows = list(reader)

        if not mode_random:
            if 'amount' not in rows[0]:
                print("❌ File CSV tidak memiliki kolom 'amount'!")
                return
            for row in rows:
                total_to_send += float(row['amount'])

            print(f"\n📊 Saldo wallet: {balance} tokens")
            print(f"📤 Total yang akan dikirim: {total_to_send} tokens")

            if balance < total_to_send:
                print("❌ Saldo tidak cukup.")
                return

    print("🚀 Mulai pengiriman token...")
    for row in rows:
        address = row['address']
        if mode_random:
            amount = round(random.uniform(min_amount, max_amount), 6)
        else:
            amount = float(row['amount'])

        success = send_token(token, address, amount, decimals)
        if not success:
            failed_addresses.append((address, amount))
        time.sleep(2)

    if failed_addresses:
        print(f"\n🔁 Ulangi pengiriman untuk {len(failed_addresses)} alamat yang gagal...")
        for address, amount in failed_addresses:
            send_token(token, address, amount, decimals)

def interactive():
    print("🧠 Multi-Sender CLI v2")

    print("\nPilih mode:")
    print("1. Kirim token SEKALI SAJA (fix amount dari CSV)")
    print("2. Kirim token TERJADWAL HARIAN (fix amount dari CSV)")
    print("3. Kirim token SEKALI SAJA dengan JUMLAH ACAK")
    mode = input("Pilihan (1/2/3): ").strip()

    token_address = input("🔗 Masukkan alamat kontrak token ERC-20: ").strip()
    csv_path = input("📄 Masukkan path file CSV (e.g. wallets.csv): ").strip()

    while not os.path.exists(csv_path):
        print("⚠️ File tidak ditemukan. Coba lagi.")
        csv_path = input("📄 Masukkan path file CSV: ").strip()

    if mode == '1':
        process_csv(csv_path, token_address)

    elif mode == '2':
        time_str = input("⏰ Masukkan waktu pengiriman harian (format HH:MM, contoh 09:30): ").strip()
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            print("❌ Format waktu salah.")
            return

        print("\n🕒 Penjadwalan aktif. Bot akan berjalan setiap hari.")
        schedule.every().day.at(time_str).do(process_csv, file_path=csv_path, token_address=token_address)

        while True:
            schedule.run_pending()
            time.sleep(30)

    elif mode == '3':
        try:
            min_amount = float(input("💰 Jumlah MINIMUM token per alamat: "))
            max_amount = float(input("💰 Jumlah MAKSIMUM token per alamat: "))
        except ValueError:
            print("❌ Input tidak valid.")
            return

        process_csv(csv_path, token_address, mode_random=True, min_amount=min_amount, max_amount=max_amount)

    else:
        print("❌ Mode tidak dikenal.")

if __name__ == "__main__":
    interactive()
