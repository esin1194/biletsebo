#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilet satis takipcisi - v3 (yanlis alarm duzeltmesi)
----------------------------------------------------
v2'deki sorun: sayfada baska sehirlerin etkinlik linkleri de bulundugu icin
script yanlis alarm veriyordu.

v3'teki degisiklikler:
  1) ONCE "yakinda satista" isaretlerine bakilir. Varsa kesinlikle alarm YOK.
  2) Satis desenleri cok daha dar. Sadece Izmir etkinligine ait sinyaller.
  3) Bubilet'in gizli fiyat alani kontrol edilir (99999 = fiyat belirlenmedi).
  4) TEYIT SARTI: iki ard arda kontrolde de satista gorunmezse alarm yok.

KURULUM
  pip install requests

CALISTIRMA
  python bilet_takip_v3.py
"""

import os
import re
import sys
import time
import random
import unicodedata
from datetime import datetime

import requests

# ============================================================
# AYARLAR
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8505580299:AAFOqt27o9rZZQgRW4j22T3k855JrcQNovc")
CHAT_ID = os.environ.get("CHAT_ID", "8689262974")

ARALIK = 30
JITTER = 8
SITE_ARASI_BEKLEME = 2

TEYIT_SAYISI = 2        # alarm icin gereken ard arda satista sonucu
HATA_ESIGI = 6
DURUM_DOSYASI = "bilet_durum_v3.txt"

# ============================================================
# ORTAK DESENLER
# ============================================================

# Bu ifadelerden biri varsa: HENUZ SATISTA DEGIL. Alarm verilmez.
BEKLEME_DESENLERI = [
    r"yakinda\s*satista",
    r"detaylar\s*cok\s*yakinda",
    r"satisa\s*acildiginda",
    r"biletler\s*henuz\s*satista\s*degil",
    r"hatirlatici\s*olustur",
    r"satista\s*degil",
    r"cok\s*yakinda\s*sat",
]

# ============================================================


def log(mesaj):
    print("[" + datetime.now().strftime("%d.%m.%Y %H:%M:%S") + "] " + mesaj, flush=True)


def sadelestir(metin):
    metin = (
        metin.replace("\u0130", "i").replace("I", "i").replace("\u0131", "i")
        .replace("\u015e", "s").replace("\u015f", "s")
        .replace("\u011e", "g").replace("\u011f", "g")
        .replace("\u00dc", "u").replace("\u00fc", "u")
        .replace("\u00d6", "o").replace("\u00f6", "o")
        .replace("\u00c7", "c").replace("\u00e7", "c")
    )
    metin = unicodedata.normalize("NFKD", metin)
    metin = "".join(c for c in metin if not unicodedata.combining(c))
    return metin.lower()


def telegram_gonder(mesaj, sessiz=False):
    if "BURAYA" in BOT_TOKEN or "BURAYA" in CHAT_ID:
        log("UYARI: Telegram ayarlari yapilmamis. Mesaj:")
        log(mesaj)
        return False
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": mesaj,
                "parse_mode": "HTML",
                "disable_notification": sessiz,
            },
            timeout=15,
        )
        if r.status_code == 200:
            return True
        log("Telegram hatasi: " + str(r.status_code))
        return False
    except Exception as e:
        log("Telegram gonderilemedi: " + str(e))
        return False


BASLIKLAR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def sayfa_al(url):
    r = requests.get(url, headers=BASLIKLAR, timeout=25)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def bekliyor_mu(duz):
    """Sayfada 'henuz satista degil' isareti var mi?"""
    for desen in BEKLEME_DESENLERI:
        if re.search(desen, duz, re.IGNORECASE):
            return True
    return False


# ------------------------------------------------------------
# SITE KONTROL FONKSIYONLARI
# Her biri doner: (durum, link)
#   durum: "SATISTA" | "BEKLIYOR" | "BELIRSIZ" | "HATA"
# ------------------------------------------------------------


def kontrol_bubilet_sanatci():
    """
    Sanatci sayfasi. Satista olan etkinlikler /sehir/etkinlik/.../seans/ID
    formatinda linklenir. Sadece IZMIR linkini ariyoruz.
    """
    url = "https://www.bubilet.com.tr/sanatci/sebnem-ferah"
    try:
        ham = sayfa_al(url)
    except Exception as e:
        return "HATA", str(e)[:150]

    yol = re.search(r'href="(/izmir/etkinlik/[^"]*?/seans/\d+)"', ham)
    if yol:
        return "SATISTA", "https://www.bubilet.com.tr" + yol.group(1)

    duz = sadelestir(ham)
    if "izmir" in duz:
        return "BEKLIYOR", ""
    return "BELIRSIZ", ""


def kontrol_bubilet_izmir():
    """
    Dogrudan Izmir etkinlik sayfasi.
    ONEMLI: Sayfada baska sehirlerin ve baska konserlerin linkleri de var.
    Bu yuzden link aramiyoruz; sayfanin KENDI durum isaretlerine bakiyoruz.
    """
    url = "https://www.bubilet.com.tr/izmir/etkinlik/sebnem-ferah-"
    try:
        ham = sayfa_al(url)
    except Exception as e:
        return "HATA", str(e)[:150]

    duz = sadelestir(ham)

    # 1) Once bekleme isaretleri
    if bekliyor_mu(duz):
        return "BEKLIYOR", ""

    # 2) Bubilet fiyat alani. 99999 = fiyat henuz belirlenmedi.
    fiyat = re.search(
        r'product:price:amount[^0-9]{0,40}(\d+)', ham, re.IGNORECASE
    )
    if fiyat:
        deger = int(fiyat.group(1))
        if deger >= 99999:
            return "BEKLIYOR", ""
        # Gercek fiyat gorundu -> satista
        return "SATISTA", url

    # 3) Sepete ekleme / satin alma butonu
    if re.search(r"sepete\s*ekle|hemen\s*al\b|satin\s*al\b", duz):
        return "SATISTA", url

    return "BELIRSIZ", ""


def kontrol_biletix():
    """
    Biletix sanatci grup sayfasi.
    DIKKAT: Sayfanin menusunde 'Izmir / Ege' bolge secenegi var. Bu yuzden
    metinde 'izmir' aramak yanlis alarma yol acar.
    Sadece GERCEK etkinlik linkini ariyoruz. Ankara ornegi:
      /etkinlik/5PSF4/TURKIYE/tr/sebnem-ferah-02-10-2026-ankara
    Izmir icin beklenen:
      /etkinlik/XXXXX/TURKIYE/tr/sebnem-ferah-18-09-2026-izmir
    """
    url = "https://www.biletix.com/etkinlik-grup/552666920/TURKIYE/tr/sebnem-ferah"
    try:
        ham = sayfa_al(url)
    except Exception as e:
        return "HATA", str(e)[:150]

    m = re.search(
        r'href="([^"]*?/etkinlik/[^"]*?sebnem-ferah-\d{2}-\d{2}-\d{4}-izmir[^"]*)"',
        ham,
        re.IGNORECASE,
    )
    if not m:
        # Izmir etkinligi sayfada hic yok -> henuz eklenmemis
        return "BEKLIYOR", ""

    yol = m.group(1)
    if yol.startswith("http"):
        link = yol
    else:
        link = "https://www.biletix.com" + yol

    # Link var ama tukenmis olabilir. Linkin cevresindeki metne bak.
    bas = max(0, m.start() - 300)
    son = min(len(ham), m.end() + 300)
    cevre = sadelestir(ham[bas:son])

    if re.search(r"tukendi|satis\s*yok", cevre):
        return "BEKLIYOR", ""
    if bekliyor_mu(cevre):
        return "BEKLIYOR", ""

    return "SATISTA", link


def kontrol_paribu():
    """Paribu Pass sanatci sayfasi."""
    url = "https://pass.paribu.com/muzik/sebnem-ferah"
    try:
        ham = sayfa_al(url)
    except Exception as e:
        return "HATA", str(e)[:150]

    duz = sadelestir(ham)

    if bekliyor_mu(duz):
        return "BEKLIYOR", ""

    if re.search(r"izmir\s*arena[\s\S]{0,120}?(bilet\s*al|satin\s*al|sepete\s*ekle)", duz):
        return "SATISTA", url

    return "BELIRSIZ", ""


SITELER = [
    {"ad": "Bubilet (sanatci)", "fn": kontrol_bubilet_sanatci},
    {"ad": "Bubilet (Izmir sayfasi)", "fn": kontrol_bubilet_izmir},
    {"ad": "Biletix", "fn": kontrol_biletix},
    {"ad": "Paribu Pass", "fn": kontrol_paribu},
]

# ============================================================


def durum_oku():
    if os.path.exists(DURUM_DOSYASI):
        with open(DURUM_DOSYASI, "r", encoding="utf-8") as f:
            return set(x.strip() for x in f if x.strip())
    return set()


def durum_ekle(deger):
    with open(DURUM_DOSYASI, "a", encoding="utf-8") as f:
        f.write(deger + "\n")


def alarm_gonder(site_adi, link):
    mesaj = (
        "\U0001F6A8\U0001F6A8\U0001F6A8 <b>BILETLER SATISTA</b> "
        "\U0001F6A8\U0001F6A8\U0001F6A8\n\n"
        "<b>" + site_adi + "</b>\n"
        "Izmir konseri satisa acildi.\n\n"
        '<a href="' + link + '">HEMEN BILET AL</a>\n\n' + link
    )
    for _ in range(3):
        telegram_gonder(mesaj)
        time.sleep(1)


def main():
    log("=" * 55)
    log("BILET TAKIBI v3 BASLIYOR")
    for s in SITELER:
        log("  - " + s["ad"])
    log("Aralik: ~" + str(ARALIK) + " sn")
    log("Alarm icin " + str(TEYIT_SAYISI) + " ard arda teyit gerekiyor")
    log("=" * 55)

    telegram_gonder(
        "\U0001F3AB <b>Bilet takibi basladi (v3)</b>\n\n"
        "Izlenen site: <b>" + str(len(SITELER)) + "</b>\n"
        + "\n".join("- " + s["ad"] for s in SITELER)
        + "\n\nAralik: " + str(ARALIK) + " sn\n"
        "Yanlis alarm korumasi: acik\n"
        "Hedef: <b>18 Eylul - Izmir Arena</b>",
        sessiz=True,
    )

    bildirilenler = durum_oku()
    teyit = dict((s["ad"], 0) for s in SITELER)
    hata_sayaci = dict((s["ad"], 0) for s in SITELER)
    uyarildi = dict((s["ad"], False) for s in SITELER)
    tur = 0

    while True:
        tur += 1
        ozet = []

        for site in SITELER:
            ad = site["ad"]
            try:
                durum, link = site["fn"]()
            except Exception as e:
                durum, link = "HATA", str(e)[:150]

            if durum == "SATISTA":
                hata_sayaci[ad] = 0
                teyit[ad] += 1
                if teyit[ad] < TEYIT_SAYISI:
                    ozet.append(ad + ": SATISTA? teyit " + str(teyit[ad]))
                    log("Olasi satis: " + ad + " -- teyit bekleniyor ("
                        + str(teyit[ad]) + "/" + str(TEYIT_SAYISI) + ")")
                else:
                    if ad not in bildirilenler:
                        log(">>> ONAYLANDI - SATISTA: " + ad + " -> " + link)
                        alarm_gonder(ad, link)
                        durum_ekle(ad)
                        bildirilenler.add(ad)
                    ozet.append(ad + ": SATISTA")

            elif durum == "BEKLIYOR":
                hata_sayaci[ad] = 0
                teyit[ad] = 0
                uyarildi[ad] = False
                ozet.append(ad + ": bekliyor")

            elif durum == "BELIRSIZ":
                hata_sayaci[ad] = 0
                teyit[ad] = 0
                ozet.append(ad + ": belirsiz")

            else:
                teyit[ad] = 0
                hata_sayaci[ad] += 1
                ozet.append(ad + ": hata(" + str(hata_sayaci[ad]) + ")")
                if hata_sayaci[ad] == HATA_ESIGI and not uyarildi[ad]:
                    telegram_gonder(
                        "\u26A0\uFE0F <b>Baglanti uyarisi</b>\n\n"
                        + ad + " sayfasina " + str(HATA_ESIGI)
                        + " kez ust uste ulasilamadi.\n\nSon hata: " + link
                    )
                    uyarildi[ad] = True

            time.sleep(SITE_ARASI_BEKLEME)

        if tur % 10 == 1 or any("SATISTA" in x for x in ozet):
            log(" | ".join(ozet))

        bekleme = max(12, ARALIK + random.randint(-JITTER, JITTER))
        time.sleep(bekleme)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Takip durduruldu.")
        sys.exit(0)
