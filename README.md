


***




# 🔁 ERC20 Token Multisender Bot

Bot Python untuk mengirim token ERC20 ke banyak wallet sekaligus menggunakan file `.csv`.

## 📦 Fitur

- Konversi otomatis alamat wallet ke format checksum
- Pengiriman token dalam rentang jumlah acak
- Pengiriman langsung atau terjadwal
- Logging transaksi otomatis
- Tampilan CLI interaktif menggunakan `rich`

---

## 🪰 Persyaratan VPS

- Ubuntu 20.04+ (rekomendasi)
- Python 3.8+
- Internet aktif
- Akses ke file `.env` berisi konfigurasi pribadi (private key, token contract, dll)

---

## 🛠️ Langkah Instalasi

### 1. 🔄 Update sistem & install dependensi

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

### 2. 📁 Clone repository

```bash
git clone https://github.com/zheonebro/Multisender_bot.git
cd erc20-multisender
```



### 3. 🧪 Buat virtual environment dan aktifkan

```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. 📦 Install library yang dibutuhkan

```bash
pip install -r requirements.txt
```

> Pastikan `requirements.txt` berisi:  
> `web3`, `python-dotenv`, `schedule`, `rich`

### 5. ⚙️ Konfigurasi file `.env`

Buat file `.env` di folder utama dan isi dengan:

```env
PRIVATE_KEY=0xPRIVATEKEYANDA
SENDER_ADDRESS=0xAlamatWalletAnda
INFURA_URL=https://mainnet.infura.io/v3/YOUR-PROJECT-ID
TOKEN_CONTRACT=0xTokenContractAddress
```

> ***untuk infura URL tea sepolia tesnet gunakan RPC dibawah ini***

```
https://tea-sepolia.g.alchemy.com/v2/yN8jExL8zpeSAT-d20KX1obM239S83Lc
```


### 6. 📁 Siapkan file wallet `.csv`

Buat file `wallets.csv` dengan format seperti ini:

```csv
address
0xAbcd1234...
0xEfgh5678...
...
```

> Baris pertama harus `address` (header)

---

## 🚀 Menjalankan Bot

### Jalankan script utama

```bash
python3 main.py
```

Ikuti instruksi interaktif untuk:

- Menentukan jumlah token minimum & maksimum
- Memilih mode: langsung / terjadwal

---

## 📝 Log & Output

- Semua transaksi berhasil/gagal dicatat di file `logs.txt`
- Alamat valid dikonversi otomatis ke `wallets_checksummed.csv`
- Transaksi ditampilkan di terminal dan bisa diklik langsung ke Etherscan

---

## 🧽 Tips Tambahan

- Gunakan VPS dengan resource cukup untuk pengiriman skala besar.
- Jangan kirim lebih banyak token dari saldo Anda 😅.
- Periksa gas fee sebelum mengirim batch besar.

---



