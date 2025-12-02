# 🇹🇷 AI Destekli GTIP Sınıflandırma & Vergi Asistanı

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![Gradio](https://img.shields.io/badge/UI-Gradio-orange) ![Gemini AI](https://img.shields.io/badge/AI-Google%20Gemini-purple)

Bu proje, kimyasal ürünlerin ve hammaddelerin **Gümrük Tarife İstatistik Pozisyonu (GTIP)** sınıflandırmasını otomatikleştirmek, gümrük vergilerini analiz etmek ve geçmiş emsalleri yönetmek için geliştirilmiş kapsamlı bir yapay zeka asistanıdır.

**Google Gemini Pro/Flash** modellerini kullanarak SDS (Güvenlik Bilgi Formu) ve etiket görsellerini analiz eder, mevzuata uygun GTIP önerileri sunar.

## 🚀 Özellikler

* **🧠 Yapay Zeka Destekli Sınıflandırma:** Ürün görsellerini (PDF/JPG) ve metin girdilerini analiz ederek GTIP kodu, tanımı ve gerekçesi sunar.
* **⚡ Toplu (Batch) İşlem:** Çoklu dosya yükleme desteği ve **Multithreading** mimarisi ile aynı anda birden fazla dosyanın hızlı analizi.
* **🏛️ Vergi & Mevzuat Asistanı:** Sipariş listeleri ile bileşen listelerini (Excel) eşleştirir, *V Sayılı Liste* veritabanında tarama yaparak vergi risklerini raporlar.
* **🔍 Akıllı Emsal Arama:** Geçmişte yapılan sınıflandırmalar içinde (JSONL veritabanı) anlık arama yapar.
* **📷 OCR & Görsel Okuma:** Poppler entegrasyonu ile PDF ve görsellerden metin çıkarımı.
* **🛡️ Güvenli Veri Kaydı:** `threading.Lock` mekanizması ile veritabanına (cases.jsonl) eşzamanlı ve kayıpsız yazma.
* **📊 İnteraktif Arayüz:** Gradio tabanlı modern ve kullanıcı dostu web arayüzü.

## 🛠️ Kurulum

Projeyi yerel makinenizde çalıştırmak için aşağıdaki adımları izleyin.

### Gereksinimler
* Python 3.9 veya üzeri
* Poppler (PDF işlemleri için)
* Google Gemini API Anahtarı

### Adım Adım Kurulum

1.  **Depoyu Klonlayın:**
    ```bash
    git clone [https://github.com/KULLANICI_ADIN/GTIP-Asistani.git](https://github.com/KULLANICI_ADIN/GTIP-Asistani.git)
    cd GTIP-Asistani
    ```

2.  **Sanal Ortamı Oluşturun:**
    ```bash
    python -m venv env
    # Windows için:
    .\env\Scripts\activate
    # Mac/Linux için:
    source env/bin/activate
    ```

3.  **Kütüphaneleri Yükleyin:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Poppler Ayarı:**
    * Proje dizinine `poppler` klasörünü ekleyin veya sistem yoluna tanımlayın.
    * *Not: EXE derlemesi için `poppler/Library/bin` yolu kullanılır.*

5.  **Uygulamayı Başlatın:**
    ```bash
    python Application.py
    ```

## ⚙️ Yapılandırma

Uygulama arayüzündeki **"Ayarlar"** sekmesinden Google Gemini API anahtarınızı giriniz. Anahtar `config.json` dosyasına şifrelenmeden kaydedilir (bu dosyayı git reposuna göndermeyiniz).

## 📦 EXE (Executable) Oluşturma

Projeyi tek bir `.exe` dosyası haline getirmek için **PyInstaller** kullanılır. Gradio 5.x ve Groovy bağımlılıklarını içeren optimize edilmiş build komutu:

```bash
pyinstaller --noconfirm --onedir --console --name "GTIP_Asistani" --clean \
 --collect-all gradio \
 --collect-all gradio_client \
 --collect-all safehttpx \
 --collect-all groovy \
 --hidden-import=openpyxl \
 --hidden-import=pdf2image \
 --add-data "poppler/Library/bin;poppler_bin" \
 Application.py
 ```

## 📂 Proje Yapısı
GTIP-Asistani/
├── Application.py       # Ana uygulama dosyası
├── cases.jsonl          # Sınıflandırılmış emsal veritabanı
├── vergi_listesi.jsonl  # Gümrük vergi listesi (Cache)
├── config.json          # API anahtarı ve model ayarları
├── poppler/             # PDF işleme motoru
└── gecmis_taramalar/    # Log dosyaları


## 🤝 Katkıda Bulunma
Bu depoyu Fork'layın.

Yeni bir özellik dalı (feature branch) oluşturun (git checkout -b yeni-ozellik).

Değişikliklerinizi Commit edin (git commit -m 'Yeni özellik eklendi').

Dalınızı Push edin (git push origin yeni-ozellik).

Bir Pull Request oluşturun.

## 📝 Lisans
Bu proje MIT lisansı ile lisanslanmıştır.

Geliştirici: [Bekir Can Yalçın]


### Ekstra Tavsiye: `requirements.txt` Oluşturma
Bu README dosyasında `pip install -r requirements.txt` komutu geçiyor. Bunu oluşturmak için terminale şunu yazmayı unutma:

```bash
pip freeze > requirements.txt
