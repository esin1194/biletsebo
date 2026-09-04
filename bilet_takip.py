#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bubilet - Sebnem Ferah / Izmir Arena takipcisi
Etkinligin durum bolumundeki HER degisikligi bildirir:
satis tarihi duyurusu, satisin acilmasi, fiyat bilgisi vs.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

ETKINLIK_URL = "https://www.bubilet.com.tr/izmir/etkinlik/sebnem-ferah-"
DURUM_DOSYASI = "durum.json"

DONGU_SAYISI = 12
DONGU_ARALIGI = 20

KESME_NOKTALARI = [
    "Etkinlik Kurallari", "Etkinlik Kuralları",
    "Degerlendirmeler", "Değerlendirmeler",
    "Mekandaki Diger", "Mekandaki Diğer",
]

AYLAR = ["ocak", "subat", "şubat", "mart", "nisan", "mayis", "mayıs",
         "haziran", "temmuz", "agustos", "ağustos", "eylul", "eylül",
         "ekim", "kasim", "kasım", "aralik", "aralık"]

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


def telegram_gonder(metin):
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    if not token or not chat_id:
        log("HATA: BOT_TOKEN veya CHAT_ID yok.")
        return False
    try:
        cevap = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": metin, "parse_mode": "HTML"},
            timeout=20,
        )
        if cevap.status_code == 200:
            log("Bildirim gonderildi.")
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
        return {}


def durum_yaz(veri):
    with open(DURUM_DOSYASI, "w", encoding="utf-8") as dosya:
        json.dump(veri, dosya, ensure_ascii=False, indent=2)


def etkinlik_bolumu(html):
    """Etkinligin kendi bolumunu cikarir, alt reklamlari atar."""
    metin = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    metin = re.sub(r"<style.*?</style>", " ", metin, flags=re.S | re.I)
    metin = re.sub(r"<[^>]+>", " ", metin)
    metin = re.sub(r"\s+", " ", metin).strip()

    baslangic = metin.find("Izmir Arena")
    if baslangic == -1:
        baslangic = metin.find("İzmir Arena")
    if baslangic == -1:
        baslangic = 0
    baslangic = max(0, baslangic - 300)

    parca = metin[baslangic:baslangic + 1200]

    for isaret in KESME_NOKTALARI:
        yer = parca.find(isaret)
        if yer > 100:
            parca = parca[:yer]
            break

    return parca.strip()


def tarih_var_mi(metin):
    """Bolumde satis tarihi gibi duran bir ifade var mi."""
    kucuk = metin.lower()
    if re.search(r"\d{1,2}[./]\d{1,2}", kucuk):
        return True
    if re.search(r"\d{1,2}:\d{2}", kucuk):
        return True
    if "satista" in kucuk or "satışta" in kucuk:
        if not ("yakinda" in kucuk or "yakında" in kucuk):
            return True
    for ay in AYLAR:
        if re.search(rf"satis\w*\s+\d{{1,2}}\s+{ay}", kucuk):
            return True
    return False


def main():
    veri = durum_oku()
    onceki = veri.get("bolum", "")

    guncel = ""
    for tur in range(DONGU_SAYISI):
        try:
            cevap = requests.get(ETKINLIK_URL, headers=BASLIKLAR, timeout=25)
            cevap.raise_for_status()
            guncel = etkinlik_bolumu(cevap.text)
            log(f"Kontrol {tur + 1}/{DONGU_SAYISI}: {len(guncel)} karakter")

            if onceki and guncel and guncel != onceki:
                log("DEGISIKLIK VAR")
                break
        except Exception as hata:
            log(f"Hata: {hata}")

        if tur < DONGU_SAYISI - 1:
            time.sleep(DONGU_ARALIGI)

    if not guncel:
        log("Sayfa okunamadi, cikiliyor.")
        return

    # Ilk calisma: sadece kaydet
    if not onceki:
        veri["bolum"] = guncel
        veri["ilk_kayit"] = simdi().isoformat()
        durum_yaz(veri)
        telegram_gonder(
            "✅ <b>Takip kuruldu.</b>\n\n"
            "Etkinlik sayfasindaki her degisikligi bildirecegim:\n"
            "satis tarihi duyurusu, satisin acilmasi, fiyat bilgisi.\n\n"
            "Su anki durum:\n<i>" + guncel[:300] + "</i>"
        )
        return

    if guncel != onceki:
        onemli = tarih_var_mi(guncel) and not tarih_var_mi(onceki)
        basli = "🚨 <b>SATIS TARIHI / SATIS DEGISIKLIGI!</b>" if onemli \
            else "🔔 <b>Etkinlik sayfasi degisti</b>"

        telegram_gonder(
            f"{basli}\n\n"
            "<b>YENI:</b>\n<i>" + guncel[:400] + "</i>\n\n"
            "<b>ONCEKI:</b>\n<i>" + onceki[:250] + "</i>\n\n"
            + ETKINLIK_URL
        )
        veri["bolum"] = guncel
        veri["son_degisiklik"] = simdi().isoformat()
        durum_yaz(veri)
        return

    # Degisiklik yok - gunluk hayattayim mesaji
    bugun = simdi().strftime("%Y-%m-%d")
    if veri.get("son_heartbeat") != bugun and simdi().hour >= 10:
        telegram_gonder("✅ Takip calisiyor. Sayfada henuz degisiklik yok.")
        veri["son_heartbeat"] = bugun

    veri["son_kontrol"] = simdi().isoformat()
    durum_yaz(veri)


if __name__ == "__main__":
    try:
        main()
    except Exception as hata:
        log(f"Beklenmeyen hata: {hata}")
        sys.exit(0)
