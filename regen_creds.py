"""
regen_creds.py
==============
Regenerate Polymarket API credentials dari private key.
Jalankan: python regen_creds.py

Script ini akan:
1. Derive signer address dari private key
2. Generate/derive API credentials baru
3. Test credentials dengan get_balance_allowance
4. Tanya apakah mau update .env otomatis
"""

import os
import sys
import json
import time
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
FUNDER      = os.getenv("POLYMARKET_FUNDER", "")

CLOB_BASE = "https://clob.polymarket.com"

print()
print("="*55)
print("  POLYMARKET — REGENERATE API CREDENTIALS")
print("="*55)

if not PRIVATE_KEY:
    print("\n❌ POLYMARKET_PRIVATE_KEY tidak ada di .env")
    sys.exit(1)

print(f"\nPrivate key : {PRIVATE_KEY[:8]}...{PRIVATE_KEY[-4:]}")
print(f"Funder      : {FUNDER or 'kosong'}")

# ── Derive signer address ─────────────────────────────────────
try:
    from eth_account import Account
    acct = Account.from_key(PRIVATE_KEY)
    signer_addr = acct.address
    print(f"Signer addr : {signer_addr}")
except Exception as e:
    print(f"❌ Tidak bisa derive signer address: {e}")
    sys.exit(1)

# ── Import py_clob_client ─────────────────────────────────────
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
except ImportError:
    print("❌ py-clob-client belum terinstall")
    print("   Jalankan: pip install py-clob-client")
    sys.exit(1)

# ── Generate credentials untuk setiap sig_type ───────────────
print("\n[1] Generate/derive credentials...")

best_creds = None
best_sig_type = None
best_balance = -1.0

for sig_type in [1, 2, 0]:
    label = {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE"}[sig_type]
    try:
        # Init L1 client (tanpa creds dulu)
        client_l1 = ClobClient(
            host=CLOB_BASE,
            key=PRIVATE_KEY,
            chain_id=137,
            funder=FUNDER or signer_addr,
            signature_type=sig_type,
        )
        # Derive credentials dari private key
        creds_raw = client_l1.create_or_derive_api_creds()

        # Parse response
        if isinstance(creds_raw, dict):
            api_key  = creds_raw.get("api_key") or creds_raw.get("apiKey", "")
            api_sec  = creds_raw.get("api_secret") or creds_raw.get("secret", "")
            api_pass = creds_raw.get("api_passphrase") or creds_raw.get("passphrase", "")
        else:
            api_key  = getattr(creds_raw, "api_key", "")
            api_sec  = getattr(creds_raw, "api_secret", "")
            api_pass = getattr(creds_raw, "api_passphrase", "")

        if not api_key:
            print(f"  sig_type={sig_type} ({label}): ❌ credentials kosong")
            continue

        print(f"  sig_type={sig_type} ({label}): got key={api_key[:8]}...")

        # Test credentials dengan get_balance_allowance
        creds_obj = ApiCreds(
            api_key=api_key,
            api_secret=api_sec,
            api_passphrase=api_pass,
        )
        client_l2 = ClobClient(
            host=CLOB_BASE,
            key=PRIVATE_KEY,
            chain_id=137,
            creds=creds_obj,
            funder=FUNDER or signer_addr,
            signature_type=sig_type,
        )
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp   = client_l2.get_balance_allowance(params)

        # Parse balance
        raw_bal = None
        if isinstance(resp, dict):
            raw_bal = resp.get("balance")
            if isinstance(raw_bal, dict):
                raw_bal = (raw_bal.get("decimal")
                           or raw_bal.get("value")
                           or raw_bal.get("balance"))
        balance = float(raw_bal) if raw_bal is not None else 0.0

        print(f"    ✅ Balance: ${balance:.4f}")
        print(f"    Raw resp : {json.dumps(resp)[:200]}")

        if best_creds is None or balance > best_balance:
            best_creds    = (api_key, api_sec, api_pass)
            best_sig_type = sig_type
            best_balance  = balance

    except Exception as e:
        err = str(e)
        if "401" in err or "Unauthorized" in err:
            print(f"  sig_type={sig_type} ({label}): ❌ 401 Unauthorized (sig_type mungkin salah untuk akun ini)")
        else:
            print(f"  sig_type={sig_type} ({label}): ❌ {type(e).__name__}: {err[:120]}")

# ── Hasil ─────────────────────────────────────────────────────
print()
if best_creds is None:
    print("="*55)
    print("❌ SEMUA sig_type gagal!")
    print()
    print("Kemungkinan penyebab:")
    print("1. FUNDER address salah")
    print("   → Buka polymarket.com/profile")
    print("   → Copy alamat wallet proxy yang tampil")
    print("   → Update POLYMARKET_FUNDER= di .env")
    print()
    print("2. Private key bukan dari akun yang terdaftar di Polymarket")
    print("   → Pastikan private key = akun yang sudah pernah login ke polymarket.com")
    print()
    print("3. Akun belum pernah buat API key sebelumnya")
    print("   → Login ke polymarket.com")
    print("   → Settings → API Keys → Create Key")
    sys.exit(1)

api_key, api_sec, api_pass = best_creds
label = {0: "EOA", 1: "POLY_PROXY", 2: "GNOSIS_SAFE"}[best_sig_type]

print("="*55)
print(f"✅ Credentials valid! sig_type={best_sig_type} ({label}), balance=${best_balance:.4f}")
print()
print("Credentials untuk .env:")
print("-"*50)
print(f"POLYMARKET_API_KEY={api_key}")
print(f"POLYMARKET_API_SECRET={api_sec}")
print(f"POLYMARKET_API_PASSPHRASE={api_pass}")
print("-"*50)

# ── Tanya update .env ─────────────────────────────────────────
ans = input("\nUpdate .env otomatis? (y/n): ").strip().lower()
if ans == 'y':
    _update_env(".env", api_key, api_sec, api_pass, best_sig_type)
else:
    print("\nCopy manual ke .env ya.")
    if best_sig_type != 1:
        print(f"\n⚠️  Juga tambahkan/update di executor/polymarket.py _init():")
        print(f"   signature_type={best_sig_type},  # {label}")
print()


def _update_env(env_path, api_key, api_sec, api_pass, sig_type):
    import re
    if not os.path.exists(env_path):
        print(f"❌ File {env_path} tidak ditemukan")
        return
    with open(env_path, "r") as f:
        content = f.read()

    def set_key(content, key, value):
        pattern = rf"^{re.escape(key)}=.*$"
        replacement = f"{key}={value}"
        if re.search(pattern, content, re.MULTILINE):
            return re.sub(pattern, replacement, content, flags=re.MULTILINE)
        return content + f"\n{key}={value}"

    content = set_key(content, "POLYMARKET_API_KEY", api_key)
    content = set_key(content, "POLYMARKET_API_SECRET", api_sec)
    content = set_key(content, "POLYMARKET_API_PASSPHRASE", api_pass)

    with open(env_path, "w") as f:
        f.write(content)
    print(f"✅ .env diupdate!")
    print(f"\n⚠️  Juga update executor/polymarket.py — di method _init():")
    print(f"   Cari baris: self._client = ClobClient(")
    print(f"   Tambahkan : signature_type={sig_type},  # {label}")
