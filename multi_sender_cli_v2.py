import csv
import os
import time
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
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
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

def process_csv(file_path, token_address):
    token = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=erc20_abi)
    decimals = token.functions.decimals().call()
    balance = check_balance(token, decimals)
    total_to_send = 0
    failed_addresses = []

    with open(file_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total_to_send += float(row['amount'])

    print(f"\n📊 Saldo wallet: {balance} tokens")
    print(f"📤 Total yang akan dikirim: {total_to_send} tokens")

    if balance < total_to_send:
        print("❌ Saldo tidak cukup untuk mengirim semua token.")
        return

    with open(file_path, "r") as file:
        reader = csv.DictReader(file)
        for row in reader:
            success = send_token(token, row['address'], float(row['amount']), decimals)
            if not success:
                failed_addresses.append(row)

            time.sleep(2)

    # Retry all failed after loop (opsional)
    if failed_addresses:
        print(f"\n🔁 Mengulang pengiriman untuk {len(failed_addresses)} alamat yang gagal...")
        for row in failed_addresses:
            send_token(token, row['address'], float(row['amount']), decimals)

def interactive():
    print("🧠 Multi-Sender CLI v2")

    print("\nPilih mode:")
    print("1. Kirim token SEKALI SAJA")
    print("2. Kirim token TERJADWAL HARIAN")
    mode = input("Pilihan (1/2): ").strip()

    token_address = input("🔗 Masukkan alamat kontrak token ERC-20: ").strip()
    csv_path = input("📄 Masukkan path file CSV wallet (e.g. wallets.csv): ").strip()

    while not os.path.exists(csv_path):
        print("⚠️ File tidak ditemukan. Coba lagi.")
        csv_path = input("📄 Masukkan path file CSV wallet: ").strip()

    if mode == '1':
        print("\n📦 Menjalankan pengiriman token sekarang...")
        process_csv(csv_path, token_address)
    elif mode == '2':
        time_str = input("⏰ Masukkan waktu pengiriman harian (format HH:MM, contoh 09:30): ").strip()
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            print("⚠️ Format waktu salah.")
            return

        print("\nKonfirmasi Pengaturan:")
        print(f"📄 File CSV      : {csv_path}")
        print(f"🔗 Token Address : {token_address}")
        print(f"⏰ Jadwal Harian : {time_str}")
        konfirmasi = input("Lanjutkan? (y/n): ").lower()

        if konfirmasi != 'y':
            print("❌ Dibatalkan oleh pengguna.")
            return

        print("✅ Bot dijadwalkan berjalan setiap hari.")
        schedule.every().day.at(time_str).do(process_csv, file_path=csv_path, token_address=token_address)

        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        print("❌ Mode tidak dikenal.")

if __name__ == "__main__":
    interactive()
