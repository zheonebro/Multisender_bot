# Multisender_bot

1. Pengiriman token ke banyak address (dari file CSV)
2. Validasi & konversi checksum otomatis
3. Pengiriman langsung atau terjadwal harian
4.Tampilan CLI interaktif (dengan rich)
5.Estimasi token yang dibutuhkan
6. Logging transaksi & error



sebelum memulai buat screen terlebih dahulu dengan perintah 

screen -S Namafile

Contoh :
```screen -S multisender```



⚙️ 1. Clone Repository & Install Library
# 1. Update & install git + pip
```sudo apt update && sudo apt install -y git python3-pip```

# 2. Clone repository (ganti URL jika pakai Git pribadi)
```git clone https://github.com/zheonebro/Multisender_bot.git```

```cd multisender-bot```

# 3. Install library yang dibutuhkan
```pip3 install -r requirements.txt```

====================================================================================
Agar program PIP3 tidak bentrok dengan yang lain lakukan perintah berikut :
# I. Pastikan pip & venv tersedia
```sudo apt install python3-pip python3-venv -y```

# II. Buat virtual environment
```python3 -m venv venv```

# III. Aktifkan venv
```source venv/bin/activate```

Setelah ini, prompt kamu akan berubah jadi seperti ini:

(venv) user@vps:~/multisender-bot$


=====================================================================================


2. Konfigurasi .env
Buat file .env di dalam folder project:

```nano .env```

Isi dengan:

```PRIVATE_KEY=0xyourprivatekey
SENDER_ADDRESS=0xYourWalletAddress
INFURA_URL=https://mainnet.infura.io/v3/YOUR_PROJECT_ID
TOKEN_CONTRACT=0xTokenContractAddress```

Note : ⚠️ Jangan pernah upload file .env ke publik!

3. Siapkan File Wallets
Buat file wallets.csv berisi address tujuan. 

```nano wallets.csv```

Contoh isi:
address
0x1234567890abcdef1234567890abcdef12345678
0xabcdefabcdefabcdefabcdefabcdefabcdefabcd

Script akan otomatis mengubah jadi checksum dan simpan ke wallets_checksummed.csv

4. Jalankan Script

```python main.py```

Ikuti petunjuk di layar:

Masukkan jumlah MIN & MAX token
( Default Min 5 , Max 20), kosongkan jika ingin menggunakan jumlah default

Pilih mode pengiriman:

1 = Sekali langsung

2 = Sekali langsung + harian ( pada saat running pertama akan melakukan pengiriman token ,setelah selesai akan berjalan sesuai jadwal yang dibuat setiap harinya 

3 = Hanya pengiriman harian

