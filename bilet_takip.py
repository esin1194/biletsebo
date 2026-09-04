#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bubilet - Sebnem Ferah / Izmir Arena bilet satis takipcisi
GitHub Actions uzerinde 7/24 calisir.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

ETKINLIK_URL = "https://www.bubilet.com.tr/izmir/etkinlik/sebnem-ferah-"
DURUM_DOSYASI = "durum.json"

DONGU_SAYISI = 6
DONGU_ARALIGI = 45

# Sayfanin alt kismindaki reklam bloklari buradan itibaren kesilir
KESME_NOKTALARI = [
    "mekandaki diger etkinlikler",
    "organizatorun diger etkinlikleri",
    "gunun en cok satanlari",
    "degerlendirmeler",
]

BEKLEME_ISARETLERI = ["yakinda satista", "yakında satışta"]
SATIS_ISARETLERI = ["sepete ekle", "kategori sec", "kategori seç", "bilet secimi"]

BASLIKLAR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
}

TR_SAAT = timezone(timedelta(hours=3))


def simdi():
    return datetime.now(TR_SAAT)


def log(mesaj):
    print(f"[{simdi().strftime('%d.%m.%Y %H:%M:%S')}] {mesaj}", flush=True)


def sadelestir(metin):
    """Turkce karakterleri sadelestirip kucult."""
    esle = {"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
            "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"}
    for eski, yeni in esle.items():
        metin = metin.replace(eski, yeni)
    return metin.lower()


def telegram_gonder(metin):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()

    if not token or not chat_id:
        log("HATA: BOT_TOKEN veya CHAT_ID tanimli degil.")
        return False

    try:
        cevap = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": metin, "parse_mode": "HTML"},
            timeout=20,
        )
        if cevap.status_code == 200:
            log("Telegram bildirimi gonderildi.")
            return True
        log(f"Telegram hatasi: {cevap.status_code}")
    except Exception as hata:
        log(f"Telegram istisnasi: {hata}")
    return False


def durum_oku():
    try:
        with open(DURUM_DOSYASI, "r", encoding="utf-8") as dosya:
            return json.load(dosya)
    except Exception:
        return {"durum": "BEKLEMEDE", "son_heartbeat": "", "supheli": 0}


def durum_yaz(veri):
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as dosya:
        json.dump(veri, dosya, ensure_ascii=False, indent=2)


def ust_bolum(html):
    """Sayfanin sadece etkinlige ait ust kismini dondurur."""
    metin = sadelestir(html)
    en_erken = len(metin)
    for isaret in KESME_NOKTALARI:
        yer = metin.find(isaret)
        if yer != -1 and yer < en_erken:
            en_erken = yer
    return metin[:en_erken]


def tek_kontrol():
    try:
        cevap = requests.get(ETKINLIK_URL, headers=BASLIKLAR, timeout=25)
        cevap.raise_for_status()
    except Exception as hata:
        log(f"Sayfa alinamadi: {hata}")
        return None, f"Sayfaya ulasilamadi: {hata}"

    bolum = ust_bolum(cevap.text)

    bekliyor = any(i in bolum for i in BEKLEME_ISARETLERI)
    satis_var = any(i in bolum for i in SATIS_ISARETLERI)

    if bekliyor:
        return False, "Yakinda Satista ibaresi hala duruyor."
    if satis_var:
        return True, "Bekleme ibaresi kalkti, satin alma alani goruldu."
    return None, "Ne bekleme ne satis isareti bulundu - sayfa degismis olabilir."


def main():
    veri = durum_oku()

    if veri.get("durum") == "SATISTA":
        log("Zaten satista olarak isaretli, cikiliyor.")
        return

    bulundu = False
    son_aciklama = ""
    belirsiz = False

    for tur in range(DONGU_SAYISI):
        sonuc, son_aciklama = tek_kontrol()
        log(f"Kontrol {tur + 1}/{DONGU_SAYISI}: {sonuc} - {son_aciklama}")

        if sonuc is True:
            bulundu = True
            break
        if sonuc is None:
            belirsiz = True

        if tur < DONGU_SAYISI - 1:
            time.sleep(DONGU_ARALIGI)

    if bulundu:
        telegram_gonder(
            "🎟 <b>BILETLER SATISTA!</b>\n\n"
            "Sebnem Ferah — 18 Eylul Cuma, 18:00\n"
            "Izmir Arena\n\n"
            f"{ETKINLIK_URL}\n\n"
            "Hemen ac, oyalanma."
        )
        veri["durum"] = "SATISTA"
        veri["bulundugu_an"] = simdi().isoformat()
        durum_yaz(veri)
        return

    if belirsiz:
        veri["supheli"] = veri.get("supheli", 0) + 1
        if veri["supheli"] == 3:
            telegram_gonder(
                "⚠️ Sayfa okunamiyor ya da yapisi degismis olabilir. "
                "Bir kez elle bakmakta fayda var:\n" + ETKINLIK_URL
            )
    else:
        veri["supheli"] = 0

    bugun = simdi().strftime("%Y-%m-%d")
    if veri.get("son_heartbeat") != bugun and simdi().hour >= 9:
        telegram_gonder("✅ Takip calisiyor. Bilet henuz satista degil.")
        veri["son_heartbeat"] = bugun

    veri["son_kontrol"] = simdi().isoformat()
    veri["son_aciklama"] = son_aciklama
    durum_yaz(veri)


if __name__ == "__main__":
    try:
        main()
    except Exception as hata:
        log(f"Beklenmeyen hata: {hata}")
        sys.exit(0)
