# TEA Sepolia Sender Bot

Bot sederhana untuk mengirim token TEA di Sepolia Testnet ke daftar wallet.

## Fitur
- Kirim token dari `wallets.csv` (berurutan/acak)
- Batas harian: 200 wallet
- Log transaksi dengan link explorer
- Rekap setiap batch
- Jadwal otomatis: 08:00 WIB

## Kebutuhan
- Python 3.8+
- RPC (misalnya Infura)
- Token TEA di wallet pengirim

## Cara Pasang
1. Clone atau salin kode:
   git clone https://github.com/zheonebro/Multisender_bot
2. Instal paket:
   pip install -r requirements.txt

## Cara Setup
1. Isi `.env`:
   PRIVATE_KEY=your_key
   SENDER_ADDRESS=your_address
   INFURA_URL=https://sepolia.infura.io/v3/your_id
   TOKEN_CONTRACT=token_address
2. Tambah wallet di `wallets.csv`:
   0x1234...

## Cara Pakai
1. Jalankan:
   python bot.py
2. Pilih:
- [1] Kirim berurutan
- [2] Kirim acak
- [3] Cek log
- [4] Jadwal harian
- [5] Ulangi gagal
- [0] Keluar
