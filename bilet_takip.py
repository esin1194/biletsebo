#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bubilet - Sebnem Ferah / Izmir Arena bilet satis takipcisi
GitHub Actions uzerinde 7/24 calisir. Bilgisayarin acik olmasi gerekmez.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------- ayarlar

ETKINLIK_URL = "https://www.bubilet.com.tr/izmir/etkinlik/sebnem-ferah-"
SANATCI_URL = "https://www.bubilet.com.tr/sanatci/sebnem-ferah"

DURUM_DOSYASI = "durum.json"

DONGU_SAYISI = 6
DONGU_ARALIGI = 45  # saniye

BEKLEME_ISARETLERI = ["yakinda satista", "yakında satışta", "satisa cikacak"]
SATIS_ISARETLERI = ["bilet al", "sepete ekle", "satin al", "kategori sec"]

BASLIKLAR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

TR_SAAT = timezone(timedelta(hours=3))


def simdi():
    return datetime.now(TR_SAAT)


def log(mesaj):
    print(f"[{simdi().strftime('%d.%m.%Y %H:%M:%S')}] {mesaj}", flush=True)


# ---------------------------------------------------------------- telegram

def telegram_gonder(metin):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if not token or not chat_id:
        log("HATA: BOT_TOKEN veya CHAT_ID tanimli degil.")
        return False

    try:
        cevap = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": metin,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if cevap.status_code == 200:
            log("Telegram bildirimi gonderildi.")
            return True
        log(f"Telegram hatasi: {cevap.status_code} {cevap.text[:200]}")
    except Exception as hata:
        log(f"Telegram istisnasi: {hata}")
    return False


# ---------------------------------------------------------------- durum

def durum_oku():
    try:
        with open(DURUM_DOSYASI, "r", encoding="utf-8") as dosya:
            return json.load(dosya)
    except Exception:
        return {"durum": "BEKLEMEDE", "son_heartbeat": "", "hata_sayaci": 0}


def durum_yaz(veri):
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as dosya:
        json.dump(veri, dosya, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- kontrol

def sayfa_al(url):
    cevap = requests.get(url, headers=BASLIKLAR, timeout=25)
    cevap.raise_for_status()
    return cevap.text


def satista_mi(html):
    metin = html.lower()

    bekliyor = any(isaret in metin for isaret in BEKLEME_ISARETLERI)
    satis_var = any(isaret in metin for isaret in SATIS_ISARETLERI)

    if bekliyor and not satis_var:
        return False, "Yakinda Satista ibaresi hala duruyor."
    if not bekliyor and satis_var:
        return True, "Bekleme ibaresi kalkti, satin alma butonu goruldu."
    if not bekliyor and not satis_var:
        return None, "Sayfada ne bekleme ne satis isareti var - yapi degismis olabilir."
    return True, "Hem bekleme hem satis isareti var - kismi acilma olabilir."


def tek_kontrol():
    try:
        html = sayfa_al(ETKINLIK_URL)
    except Exception as hata:
        log(f"Sayfa alinamadi: {hata}")
        return None, f"Sayfaya ulasilamadi: {hata}"

    sonuc, aciklama = satista_mi(html)

    if sonuc is None:
        try:
            html2 = sayfa_al(SANATCI_URL)
            if "izmir" in html2.lower():
                sonuc2, aciklama2 = satista_mi(html2)
                if sonuc2 is not None:
                    return sonuc2, f"Sanatci sayfasindan: {aciklama2}"
        except Exception:
            pass

    return sonuc, aciklama


# ---------------------------------------------------------------- ana akis

def main():
    veri = durum_oku()
    onceki = veri.get("durum", "BEKLEMEDE")
    log(f"Onceki durum: {onceki}")

    if onceki == "SATISTA":
        log("Bilet zaten acilmis olarak isaretli. Bildirim tekrarlanmayacak.")
        return

    bulundu = False
    son_aciklama = ""
    hata_var = False

    for tur in range(DONGU_SAYISI):
        sonuc, son_aciklama = tek_kontrol()
        log(f"Kontrol {tur + 1}/{DONGU_SAYISI}: {sonuc} - {son_aciklama}")

        if sonuc is True:
            bulundu = True
            break
        if sonuc is None:
            hata_var = True

        if tur < DONGU_SAYISI - 1:
            time.sleep(DONGU_ARALIGI)

    if bulundu:
        telegram_gonder(
            "🎟 <b>BILETLER SATISTA!</b>\n\n"
            "Sebnem Ferah — 18 Eylul Cuma, 18:00\n"
            "Izmir Arena\n\n"
            f"👉 {ETKINLIK_URL}\n\n"
            "Hemen ac, oyalanma."
        )
        veri["durum"] = "SATISTA"
        veri["bulundugu_an"] = simdi().isoformat()
        durum_yaz(veri)
        return

    bugun = simdi().strftime("%Y-%m-%d")
    if veri.get("son_heartbeat") != bugun and simdi().hour >= 9:
        telegram_gonder(
            "✅ Takip calisiyor. Bilet henuz satista degil.\n"
            "Acildigi an haber verecegim."
        )
        veri["son_heartbeat"] = bugun

    if hata_var:
        veri["hata_sayaci"] = veri.get("hata_sayaci", 0) + 1
        if veri["hata_sayaci"] in (6, 30):
            telegram_gonder(
                "⚠️ Sayfa okunamiyor ya da yapisi degismis olabilir. "
                "Bileti bir kez elle kontrol etmekte fayda var:\n"
                f"{ETKINLIK_URL}"
            )
    else:
        veri["hata_sayaci"] = 0

    veri["son_kontrol"] = simdi().isoformat()
    veri["son_aciklama"] = son_aciklama
    durum_yaz(veri)


if __name__ == "__main__":
    try:
        main()
    except Exception as hata:
        log(f"Beklenmeyen hata: {hata}")
        sys.exit(0)
