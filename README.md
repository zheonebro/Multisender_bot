# README.md — Langkah Lengkap ERC20 Multisender Bot

---

## 🚀 Deskripsi
Bot ini digunakan untuk mengirim token ERC20 ke banyak wallet secara otomatis di jaringan testnet **TEA Sepolia**. Cocok untuk airdrop massal, reward campaign, atau test distribusi token.

---

## 🛠️ Fitur
- Kirim token ke banyak address dari file CSV
- Pengiriman batch + idle otomatis antar batch
- Multi-threaded untuk efisiensi
- CLI interaktif dan juga mode otomatis
- Uji coba pengiriman ke address random
- Prevent duplikat kirim ke wallet yang sama
- Logging proses dan hasil kirim

---

## ⚙️ Step-by-Step Instalasi (untuk pemula)

### 1. Siapkan Python
Pastikan kamu sudah menginstall **Python 3.10+** di laptopmu. Jika belum:
- Windows: https://www.python.org/downloads/windows/
- Mac: https://www.python.org/downloads/mac-osx/

Pastikan `pip` sudah tersedia:
```bash
python --version
pip --version
```

### 2. Clone Repository
```bash
git clone https://github.com/zheonebro/Multisender_bot.git
cd Multisender_bot
```

### 3. Buat Virtual Environment (Opsional tapi disarankan)
```bash
python -m venv venv
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate         # Windows
```

### 4. Install Dependensi Python
```bash
pip install -r requirements.txt
```

### 📁 Tentang `requirements.txt`
File ini berisi semua library yang dibutuhkan agar script berjalan lancar. Contoh isi file:

```text
web3==6.12.0
python-dotenv==1.0.1
rich==13.7.0
tenacity==8.2.3
schedule==1.2.1
```

Untuk install semua dependensi:
```bash
pip install -r requirements.txt
```

> ✅ Gunakan versi di atas agar sesuai dengan script ini.

### 5. Siapkan File Konfigurasi `.env`
Buat file `.env` di root folder, isi seperti ini:
```ini
PRIVATE_KEY=0xPRIVATEKEY_KAMU
SENDER_ADDRESS=0xALAMAT_KAMU
INFURA_URL=https://eth-sepolia.g.alchemy.com/v2/YOUR_ALCHEMY_API_KEY
TOKEN_CONTRACT=0xKONTRAK_TOKEN_KAMU
MAX_GAS_PRICE_GWEI=50
DAILY_LIMIT=0
MIN_DELAY_SECONDS=0.5
MAX_DELAY_SECONDS=2
```

> 🔑 Gantilah semua isian dengan data milikmu.

📌 **Cara Membuat File `.env` di VPS (Linux)**
```bash
echo "PRIVATE_KEY=0x..." > .env
echo "SENDER_ADDRESS=0x..." >> .env
echo "INFURA_URL=https://eth-sepolia.g.alchemy.com/v2/..." >> .env
echo "TOKEN_CONTRACT=0x..." >> .env
echo "MAX_GAS_PRICE_GWEI=50" >> .env
echo "DAILY_LIMIT=0" >> .env
echo "MIN_DELAY_SECONDS=0.5" >> .env
echo "MAX_DELAY_SECONDS=2" >> .env
```

### 🔌 Cara Mendapatkan URL RPC TEA Sepolia dari Alchemy

Untuk bisa menghubungkan bot ke jaringan **TEA Sepolia**, kamu memerlukan **RPC URL**. RPC ini didapat dari layanan seperti **Alchemy**, langkah-langkahnya:

#### 1. Buat Akun di Alchemy
Kunjungi: https://alchemy.com  
Klik **Sign Up** dan daftar akun baru (gratis).

#### 2. Buat Project Baru
Setelah login:
- Klik tombol **“Create App”**
- Isi nama project (misal: `tea-multisender`)
- Pilih:
  - **Chain**: Ethereum
  - **Network**: Sepolia
- Klik **Create App**

#### 3. Ambil RPC URL
Setelah app dibuat:
- Klik nama project yang tadi
- Klik tab **“View Key”**
- Salin bagian **HTTPS URL**, contohnya:
```plaintext
https://eth-sepolia.g.alchemy.com/v2/abcd1234abcd1234abcd1234abcd1234
```

#### 4. Masukkan ke `.env`
Isi ENV bot kamu seperti ini:
```ini
INFURA_URL=https://eth-sepolia.g.alchemy.com/v2/abcd1234abcd1234abcd1234abcd1234
```

#### 5. (Opsional) Tes RPC URL
Kamu bisa mengetes RPC Alchemy kamu di browser atau pakai tools seperti Postman / Insomnia.

📌 **Catatan Penting:**
- Pastikan project Alchemy kamu diset ke **Ethereum - Sepolia**, bukan mainnet atau chain lain.
- Jangan gunakan URL ini untuk transaksi nyata di mainnet.
- RPC Alchemy gratis cukup untuk 300-500+ TX per hari (tergantung traffic).
- Kalau error "rate limit", coba buat project baru atau upgrade akun gratisnya.

---

## 📄 Siapkan File wallets.csv
Format file CSV seperti ini:
```csv
address
0xabc123...
0xdef456...
0x789abc...
```

📌 Cara membuat `wallets.csv` di VPS:
```bash
echo "address" > wallets.csv
echo "0xabc123..." >> wallets.csv
echo "0xdef456..." >> wallets.csv
```

File ini akan dibaca dan dikirimkan token ke semua address di dalamnya.

---

## ✅ Menjalankan Bot

### Opsi 1: Mode Interaktif
```bash
python multisender.py
```
Pilih menu berikut:
- [1] Mulai kirim token sekarang
- [2] Atur rentang jumlah token
- [3] Jadwal kirim harian (misal tiap jam 14:00)
- [4] Uji coba kirim ke address random (test)
- [5] Keluar dari program

### Opsi 2: Mode Otomatis (Loop)
```bash
python multisender.py --auto
```
Bot akan terus mengirim batch dan idle selama `IDLE_AFTER_BATCH_SECONDS` (default 300 detik).

---

## 📁 Struktur Folder
```
Multisender_bot/
├── multisender.py           # Script utama
├── .env                     # Private key dan konfigurasi
├── wallets.csv              # Daftar address tujuan
├── sent_wallets.txt         # Log address yang sudah dikirimi
├── runtime_logs/runtime.log# Log aktivitas runtime
├── requirements.txt         # Daftar dependensi
└── README.md                # Dokumentasi lengkap
```

---

## 🔐 Keamanan
- Jangan share file `.env` ke publik
- Gunakan private key hanya untuk test wallet
- Gunakan jaringan testnet (TEA Sepolia) agar aman

---

## ❗Tips Tambahan
- Gunakan RPC Alchemy/Infura agar tidak limit saat kirim banyak TX
- Kalau transaksi gagal, bot akan retry hingga 5x
- Wallet yang sudah dikirimi akan dicatat di `sent_wallets.txt` dan tidak dikirim ulang

---

Selesai! 🎉 Kamu siap menggunakan bot ini untuk mendistribusikan token ERC20 dengan mudah di testnet TEA Sepolia.

Kalau butuh contoh file `.env.example` atau `wallets.csv`, tinggal bilang ya!
