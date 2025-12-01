import google.generativeai as genai
import gradio as gr
import fastapi
import uvicorn
import json
import os
import time
from pydantic import BaseModel
from datetime import datetime
import pandas as pd
import base64
import io
from PIL import Image
import sys
import webbrowser
import re
import asyncio
from difflib import SequenceMatcher # Benzerlik hesabı için
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock 

import openpyxl
from pdf2image import convert_from_path

# --- 1. AYARLAR VE YAPILANDIRMA ---
file_writer_lock = Lock()

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

print(f"Uygulama Ana Dizini (BASE_DIR): {BASE_DIR}")

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
HISTORY_DIR = os.path.join(BASE_DIR, "gecmis_taramalar")
CLASSIFICATION_LOG_FILE = os.path.join(HISTORY_DIR, "classification_log.jsonl")
CASES_FILE = os.path.join(BASE_DIR, "cases.jsonl") 
SEARCH_LOG_FILE = os.path.join(HISTORY_DIR, "search_history.jsonl")

# Varsayılan ayarlar
DEFAULT_CONFIG = {
    "api_key": "HENUZ_GIRILMEDI_LUTFEN_AYARLAR_SEKMESINI_KULLANIN",
    "model_name": "gemini-1.5-pro-latest" 
}

def mask_api_key(api_key):
    if not api_key or "HENUZ_GIRILMEDI" in api_key or len(api_key) < 9:
        return "Geçersiz API Key (Ayarlardan Girin)"
    return f"{api_key[:4]}...{api_key[-4:]}"

# Global değişkenler
app_config = DEFAULT_CONFIG.copy()
llm_model = None

class GtipRequest(BaseModel):
    product_name: str
    composition: str
    use: str

# --- 2. YARDIMCI FONKSİYONLAR ---

def log_classification_to_history(filename, product_name, composition, ai_response_html):
    """Sınıflandırma asistanı sonuçlarını kaydeder."""
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        log_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filename": filename,
            "product_name": product_name,
            "composition": composition,
            "ai_response": ai_response_html
        }
        with open(CLASSIFICATION_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Sınıflandırma loglama hatası: {e}")

def load_config():
    global app_config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                app_config = json.load(f)
            if "model_name" not in app_config: app_config["model_name"] = DEFAULT_CONFIG["model_name"]
            print(f"Yapılandırma yüklendi. Model: {app_config['model_name']}")
        except:
            app_config = DEFAULT_CONFIG.copy()
    else:
        save_config(app_config["api_key"], app_config["model_name"])

def load_file_as_image(file_path):
    """
    Gelen dosya PDF ise ilk sayfasını JPG yapar.
    Poppler yolunu dinamik olarak (EXE içinden veya proje klasöründen) bulur.
    """
    try:
        # --- POPPLER YOLUNU BELİRLEME ---
        if getattr(sys, 'frozen', False):
            # Eğer uygulama paketlenmişse (EXE olmuşsa) geçici klasöre bak
            base_path = sys._MEIPASS
            # PyInstaller ile 'poppler_bin' adıyla paketleyeceğiz
            poppler_path = os.path.join(base_path, "poppler_bin")
        else:
            # Normal Python olarak çalışıyorsa proje klasörüne bak
            # BURAYI KENDİ KLASÖR YAPINA GÖRE KONTROL ET
            # Eğer klasör yapın: Proje/poppler/Library/bin ise:
            poppler_path = os.path.join(BASE_DIR, "poppler", "Library", "bin")
            
            # Eğer bu yol yoksa (belki direkt bin altındadır), path vermeyelim sistemdekini denesin
            if not os.path.exists(poppler_path):
                poppler_path = None 

        # Dosya uzantısını kontrol et
        if file_path.lower().endswith(".pdf"):
            # poppler_path parametresini buraya ekliyoruz
            pages = convert_from_path(file_path, dpi=300, first_page=1, last_page=1, poppler_path=poppler_path)
            if pages:
                return pages[0] 
        else:
            return Image.open(file_path)
            
    except Exception as e:
        print(f"Dosya okuma hatası ({file_path}): {e}")
        # Hata durumunda kullanıcıya bilgi vermek için None dönüyoruz
        return None

def check_tax_date_warning(date_input):
    """
    Tarihi kontrol eder, bugünden itibaren 1 yıldan (365 gün) az kaldıysa uyarı verir.
    Formatlar: '31/12/2029', '2029-12-31 00:00:00', '2029-12-31' vb.
    """
    if not date_input or str(date_input) == "nan" or str(date_input) == "-":
        return "-"
    
    try:
        # Gelen veri datetime objesi ise string'e çevir, string ise temizle
        date_str = str(date_input).replace("**", "").strip()
        
        # SORUNUN ÇÖZÜMÜ: "2025-12-31 00:00:00" gelirse boşluktan bölüp sadece ilk kısmı al
        # Bu sayede saat bilgisinden kurtuluruz.
        clean_date_part = date_str.split(" ")[0]
        
        # Tarih objesine çevir (Önce gün/ay/yıl dene, olmazsa yıl/ay/gün)
        try:
            expiry_date = datetime.strptime(clean_date_part, "%d/%m/%Y")
        except:
            # Excel genelde Yıl-Ay-Gün verir
            expiry_date = datetime.strptime(clean_date_part, "%Y-%m-%d")
            
        today = datetime.now()
        diff = expiry_date - today
        
        # Ekranda görünecek temiz tarih (saatsiz)
        display_date = expiry_date.strftime("%Y-%m-%d")
        
        # Kontroller
        if diff.days < 0:
            return f"⚫ {display_date} (SÜRESİ DOLMUŞ)"
        elif diff.days < 365:
            return f"🔴 {display_date} (KRİTİK - <1 YIL)"
        
        return display_date
        
    except Exception as e:
        # Hata durumunda (format çok farklıysa) olduğu gibi döndür ama hatayı konsola bas
        # print(f"Tarih hatası: {e}") 
        return str(date_input).split(" ")[0] # En azından saati atıp göster
    
def search_tax_db_smart(cas_no, product_name):
    """
    Vergi listesinde CAS numarası veya Kimyasal isme göre arama yapar.
    CAS numarası eşleşmesi önceliklidir.
    """
    if not os.path.exists(TAX_DB_FILE):
        return None

    best_match = None
    highest_score = 0

    # CAS Temizliği: (848) -> 848, boşlukları sil
    clean_cas = str(cas_no).replace("(", "").replace(")", "").strip() if cas_no else ""
    
    # Eğer CAS numarası çok kısaysa (örn: "2", "3") hatalı eşleşmeyi önlemek için CAS araması yapma
    is_valid_cas = len(clean_cas) > 4 and "-" in clean_cas

    target_name = product_name.lower().strip() if product_name else ""

    with open(TAX_DB_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                record = json.loads(line)
                desc = record.get("tanim", "").lower()
                gtp = record.get("gtp", "")
                
                score = 0
                
                # 1. KRİTER: CAS Numarası Eşleşmesi (Kesin Eşleşme)
                # Vergi dosyasında genelde "CAS RN 111-76-2" yazar. 
                if is_valid_cas and clean_cas in desc:
                    score += 100 
                
                # 2. KRİTER: İsim Benzerliği (CAS yoksa veya bulunamadıysa)
                elif target_name:
                    # Tam eşleşme kontrolü
                    if target_name in desc:
                         score += 60
                    else:
                        # SequenceMatcher yavaş olabilir, basit string kontrolü daha hızlıdır toplu işlemde
                        # Ancak yine de yüksek benzerlik için tutuyoruz
                        match_ratio = SequenceMatcher(None, target_name, desc).ratio()
                        if match_ratio > 0.75: # %75 üzeri benzerlik
                            score += int(match_ratio * 50)
                
                if score > highest_score and score > 50: 
                    highest_score = score
                    best_match = record
                    # CAS bulduysak döngüyü kırabiliriz, en kesin bilgi odur
                    if score >= 100: 
                        break

            except: continue
            
    return best_match

# --- YARDIMCI FONKSİYON: GEMINI BATCH ANALİZİ ---
# --- YENİ YARDIMCI: AKILLI BAĞLAM FİLTRESİ (PRE-FILTER) ---
def get_smart_tax_context(batch_products, full_tax_db_path):
    """
    2000 satırlık listeyi her seferinde göndermek yerine,
    sadece ürün isimleriyle kelime bazlı eşleşen vergi satırlarını seçer.
    Böylece prompt boyutu %95 azalır.
    """
    if not os.path.exists(full_tax_db_path):
        return ""

    # 1. Batch içindeki tüm ürünlerin isminden ANAHTAR KELİMELERİ çıkar
    search_keywords = set()
    for prod in batch_products:
        # Ürün adı ve bileşen isimlerini birleştir
        text_blob = f"{prod['name']} {' '.join(prod['ingredients'])}".lower()
        # Alfanümerik olmayanları sil, kelimelere ayır
        words = re.findall(r'\w+', text_blob)
        # 3 harften kısa kelimeleri (ve, ile, vb.) ele
        search_keywords.update([w for w in words if len(w) > 3])

    relevant_lines = []
    
    # 2. Vergi listesini tara: Anahtar kelimelerden HERHANGİ BİRİ geçiyor mu?
    try:
        with open(full_tax_db_path, 'r', encoding='utf-8') as f:
            for line in f:
                line_lower = line.lower()
                # Eğer vergi satırında, ürünün anahtar kelimelerinden biri geçiyorsa al
                if any(k in line_lower for k in search_keywords):
                    rec = json.loads(line)
                    relevant_lines.append(f"- {rec.get('tanim')} (GTIP: {rec.get('gtp')})")
    except:
        pass
    
    # Eğer hiç eşleşme bulamazsa boş dönmesin, AI şaşırır.
    # En azından "Genel kimyasallar" uyarısı ekleyelim veya boş bırakalım.
    if not relevant_lines:
        return "Bu ürün grubu için özel bir vergi kaydı bulunamadı. Genel kimya bilginle yorumla."
    
    # Çok fazla eşleşme varsa (örn: 'Asit' kelimesi 500 yerde geçiyorsa) limiti sınırla
    return "\n".join(relevant_lines[:50]) # Maksimum 50 en alakalı satır gönder

# --- GÜNCELLENMİŞ AI FONKSİYONU ---
# --- YENİ EKLENECEK FONKSİYON: EXCEL TABANLI ANALİZ ---
# --- OPTİMİZE EDİLMİŞ VERGİ ANALİZ FONKSİYONU ---
def process_tax_analysis_structured(order_file, ingredients_file):
    """
    HIZLI VERSİYON (GÜNCELLENDİ): 
    - Regex ile kesin CAS eşleşmesi yapar (Örn: 77-99-6 ararken 157577-99-6'yı bulmaz).
    - Geçerlilik tarihi 1 yıldan az ise kırmızı uyarı ekler.
    - Dosya ismine okunabilir tarih/saat ekler.
    """
    if not order_file or not ingredients_file:
        return "⚠️ Lütfen her iki Excel dosyasını da yükleyin.", None

    log_buffer = "<h3>📊 Analiz Başlatıldı... (Hızlı Mod & Hassas Eşleşme)</h3>"
    
    try:
        # --- ADIM 0: VERGİ LİSTESİNİ HAFIZAYA YÜKLEME (CACHE) ---
        tax_list_linear = []     # Düz liste
        
        if os.path.exists(TAX_DB_FILE):
            with open(TAX_DB_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        tax_list_linear.append(rec) 
                    except: continue
        
        log_buffer += f"✅ Vergi Veritabanı Önbelleğe Alındı ({len(tax_list_linear)} kayıt).<br>"

        # --- ADIM 1: SİPARİŞ VE BİLEŞEN DOSYALARINI OKUMA ---
        try:
            df_orders = pd.read_excel(order_file.name, dtype=str)
        except:
            df_orders = pd.read_csv(order_file.name, dtype=str, sep=None, engine='python')
            
        try:
            df_ing = pd.read_excel(ingredients_file.name, dtype=str)
        except:
            df_ing = pd.read_csv(ingredients_file.name, dtype=str)

        df_orders.columns = df_orders.columns.str.strip()
        df_ing.columns = df_ing.columns.str.strip()

        # Kolonları Bul
        order_col = next((c for c in df_orders.columns if "Malzeme" in c), None)
        ing_prod_col = next((c for c in df_ing.columns if "Product code" in c), None)
        ing_type_col = next((c for c in df_ing.columns if "Type" in c), None)
        ing_cas_col = next((c for c in df_ing.columns if "CAS" in c), None)
        ing_desc_col = next((c for c in df_ing.columns if "Standard description" in c), None)
        ing_pct_col = next((c for c in df_ing.columns if "Percent" in c), None)

        if not order_col or not ing_prod_col: 
            return "❌ Gerekli sütunlar (Malzeme / Product code) bulunamadı.", None

        # --- ADIM 2: BİLEŞENLERİ GRUPLAMA ---
        product_map = {}
        for _, row in df_ing.iterrows():
            p_code = str(row[ing_prod_col]).strip()
            type_val = str(row[ing_type_col]).strip()
            
            if "*" in type_val: # Sadece bileşen satırları
                if p_code not in product_map: product_map[p_code] = []
                product_map[p_code].append({
                    "cas": str(row[ing_cas_col]).strip(),
                    "name": str(row[ing_desc_col]).strip(),
                    "pct": str(row[ing_pct_col]).strip()
                })

        # --- ADIM 3: ANALİZ ---
        report_data = []
        matched_count = 0
        
        for idx, row in df_orders.iterrows():
            malzeme_kodu = str(row[order_col]).strip()
            malzeme_tanim = str(row.get("Malzeme Tanım", "")).strip()
            
            ingredients = product_map.get(malzeme_kodu, [])
            
            if not ingredients:
                report_data.append({
                    "MALZEME KODU": malzeme_kodu,
                    "ÜRÜN ADI": malzeme_tanim,
                    "BİLEŞEN": "LİSTEDE YOK",
                    "CAS NO": "-", "G.T.İ.P.": "-", "VERGİ DURUMU": "-"
                })
                continue

            for ing in ingredients:
                cas_no = ing["cas"] # Örn: 100-41-4
                chem_name = ing["name"].lower()
                
                tax_record = None
                clean_cas = cas_no.replace("(", "").replace(")", "").strip()
                
                # --- GÜNCELLENMİŞ ARAMA MANTIĞI (REGEX) ---
                # Yöntem A: CAS Numarası (Kesin Eşleşme - Regex)
                # (?<!\d) -> Öncesinde rakam YOKSA
                # (?!\d)  -> Sonrasında rakam YOKSA
                if len(clean_cas) > 4: 
                    cas_pattern = r"(?<!\d)" + re.escape(clean_cas) + r"(?!\d)"
                    for rec in tax_list_linear:
                        # Regex ile arama: "77-99-6" ararken "157577-99-6" bulmaz.
                        if re.search(cas_pattern, rec.get("tanim", "")):
                            tax_record = rec
                            break
                
                # Yöntem B: CAS ile bulunamadıysa İsim ile ara (Tam eşleşme)
                if not tax_record and len(chem_name) > 3:
                    for rec in tax_list_linear:
                        if chem_name in rec.get("tanim", "").lower():
                            tax_record = rec
                            break
                
                status = "ESLESME YOK"
                gtip = "-"
                tax_rate = "-"
                desc = "-"
                validity_display = "-"
                
                if tax_record:
                    status = "⚠️ VERGİ LİSTESİNDE"
                    gtip = tax_record.get("gtp", "-")
                    tax_rate = f"%{tax_record.get('gv_oran', '0')}"
                    desc = tax_record.get("tanim", "")
                    matched_count += 1
                    
                    # Tarih Kontrolü ve Renklendirme
                    raw_date = tax_record.get("gecerlilik", "-")
                    validity_display = check_tax_date_warning(raw_date)
                
                report_data.append({
                    "MALZEME KODU": malzeme_kodu,
                    "ÜRÜN ADI": malzeme_tanim,
                    "BİLEŞEN": ing["name"],
                    "CAS NO": cas_no,
                    "ORAN (%)": ing["pct"],
                    "VERGİ DURUMU": status,
                    "G.T.İ.P.": gtip,
                    "VERGİ ORANI": tax_rate,
                    "GEÇERLİLİK TARİHİ": validity_display, # Yeni kolon
                    "VERGİ TANIMI": desc
                })

        # --- ADIM 4: RAPORLAMA ---
        if report_data:
            df_out = pd.DataFrame(report_data)
            
            # --- DEĞİŞİKLİK BURADA: Okunabilir Tarih/Saat ---
            tarih_saat = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            output_filename = f"Vergi_Analiz_Raporu_{tarih_saat}.xlsx"
            # -----------------------------------------------
            
            output_path = os.path.join(BASE_DIR, output_filename)
            df_out.to_excel(output_path, index=False)
            
            log_buffer += f"<br>✅ <b>İşlem Tamamlandı.</b><br>"
            log_buffer += f"📦 Taranan Ürün: {len(df_orders)}<br>"
            log_buffer += f"🎯 Vergi Eşleşmesi: {matched_count}<br>"
            return log_buffer, output_path
        else:
            return "❌ Rapor oluşturulacak veri bulunamadı.", None

    except Exception as e:
        import traceback
        return f"<div style='color:red'>HATA: {str(e)} <br> {traceback.format_exc()}</div>", None


def save_config(api_key, model_name):
    global app_config
    config_data = {
        "api_key": api_key,
        "model_name": model_name
    }
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2)
        app_config = config_data
        return True
    except:
        return False
    

def create_metadata_table(files, pasted_image):
    """
    Dosyalar VEYA yapıştırılan resim değiştiğinde tabloyu yeniden oluşturur.
    """
    rows = []
    
    # 1. Dosyalar listesindekileri ekle
    if files:
        for f in files:
            rows.append([os.path.basename(f.name), "", "", ""])
            
    # 2. Yapıştırılan resim varsa onu da ekle
    if pasted_image:
        # Yapıştırılan resmin adı genelde 'image.png' gibi temp bir ad olur, biz sabit bir isim verelim
        rows.append(["Yapıştırılan_Görsel", "", "", ""])
        
    return rows

def list_available_models(api_key_input):
    """
    Girilen API anahtarı ile Google'a bağlanır ve 'generateContent' yeteneği olan modelleri listeler.
    """
    if "..." in api_key_input and api_key_input == mask_api_key(app_config.get("api_key")):
        real_key = app_config.get("api_key")
    else:
        real_key = api_key_input

    if not real_key or len(real_key) < 10:
        return gr.update(choices=[]), "⚠️ Geçersiz veya eksik API Anahtarı."

    try:
        genai.configure(api_key=real_key)
        models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                models.append(m.name)
        
        if not models:
            return gr.update(choices=[]), "⚠️ Anahtar geçerli ancak uygun model bulunamadı."
            
        return gr.update(choices=models, value=models[0], interactive=True), f"✅ Başarılı! {len(models)} model listelendi."
    except Exception as e:
        return gr.update(choices=[]), f"❌ Hata: {str(e)}"

def initialize_gemini_model():
    global llm_model
    try:
        if "HENUZ_GIRILMEDI" in app_config["api_key"]: return False
        
        genai.configure(api_key=app_config["api_key"])
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        llm_model = genai.GenerativeModel(
            model_name=app_config["model_name"],
            safety_settings=safety_settings
        )
        print(f"Gemini modeli başlatıldı: {app_config['model_name']}")
        return True
    except Exception as e:
        print(f"Model başlatma hatası: {e}")
        llm_model = None
        return False

# --- 4. GEÇMİŞ İŞLEMLERİ (GÜNCELLENDİ: HEM ARAMA HEM EMSAL GÖSTERİMİ) ---

def log_search_to_history(query, found_cases, image_obj):
    """Yapılan aramayı, bulunan ilk 3 sonucu ve varsa resmi kaydeder."""
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        
        img_str = None
        if image_obj:
            try:
                buffer = io.BytesIO()
                image_obj.save(buffer, format="JPEG", quality=70)
                img_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
            except: pass

        summary_text = ""
        if found_cases:
            for c in found_cases[:3]:
                summary_text += f"{c.get('product_name')} ({c.get('assigned_gtip')}); "

        log_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "query": query,
            "summary_results": summary_text,
            "image_b64": img_str,
            "full_results": found_cases[:5]
        }

        with open(SEARCH_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            
    except Exception as e:
        print(f"Geçmiş kaydetme hatası: {e}")

def get_filtered_history(filter_text="", history_type="Arama Geçmişi"):
    """
    GÜNCELLENDİ: Kullanıcı seçimine göre ya Arama Geçmişini ya da Kayıtlı Emsalleri getirir.
    """
    data_list = []
    raw_logs = [] # Detay gösterimi için ham veriyi tutacağız

    # --- MOD 1: ARAMA GEÇMİŞİ ---
    if history_type == "Arama Geçmişi":
        if not os.path.exists(SEARCH_LOG_FILE):
            return pd.DataFrame(columns=["Tarih", "Arama Terimi", "Sonuçlar", "Görsel"]), []
        
        try:
            with open(SEARCH_LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines):
                    if not line.strip(): continue
                    try:
                        log = json.loads(line)
                        searchable = f"{log.get('query')} {log.get('summary_results')}".lower()
                        if filter_text.lower() in searchable:
                            has_image = "📷 Var" if log.get("image_b64") else "-"
                            data_list.append([
                                log.get("timestamp"),
                                log.get("query"),
                                log.get("summary_results")[:100] + "...",
                                has_image
                            ])
                            raw_logs.append(log)
                    except: continue
            return pd.DataFrame(data_list, columns=["Tarih", "Arama Terimi", "Sonuçlar", "Görsel"]), raw_logs
        except Exception as e:
            print(f"Arama geçmişi hatası: {e}")
            return pd.DataFrame(), []

    # --- MOD 2: KAYITLI EMSALLER (DATABASE) ---
    elif history_type == "Kaydedilen Emsaller":
        if not os.path.exists(CASES_FILE):
            return pd.DataFrame(columns=["ID", "Ürün Adı", "GTIP", "Tarih"]), []
        
        try:
            with open(CASES_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines):
                    if not line.strip(): continue
                    try:
                        case = json.loads(line)
                        searchable = f"{case.get('product_name')} {case.get('assigned_gtip')} {case.get('composition_text')}".lower()
                        if filter_text.lower() in searchable:
                            data_list.append([
                                case.get("id", "-"),
                                case.get("product_name", "Bilinmiyor"),
                                case.get("assigned_gtip", "-"),
                                case.get("assignment_date", "-"),
                                case.get("composition_text", "")[:50] + "..."
                            ])
                            raw_logs.append(case)
                    except: continue
            return pd.DataFrame(data_list, columns=["ID", "Ürün Adı", "GTIP", "Tarih", "İçerik Özeti"]), raw_logs
        except Exception as e:
            print(f"Emsal okuma hatası: {e}")
            return pd.DataFrame(), []
        
    # --- MOD 3: SINIFLANDIRMA GEÇMİŞİ ---
    elif history_type == "Sınıflandırma Geçmişi":
        if not os.path.exists(CLASSIFICATION_LOG_FILE):
            return pd.DataFrame(columns=["Tarih", "Dosya Adı", "Ürün Adı", "İçerik"]), []
        
        try:
            with open(CLASSIFICATION_LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines): # En yeniden eskiye
                    if not line.strip(): continue
                    try:
                        log = json.loads(line)
                        # Arama filtresi
                        searchable = f"{log.get('filename')} {log.get('product_name')} {log.get('composition')}".lower()
                        if filter_text.lower() in searchable:
                            data_list.append([
                                log.get("timestamp"),
                                log.get("filename"),
                                log.get("product_name"),
                                log.get("composition")
                            ])
                            raw_logs.append(log)
                    except: continue
            return pd.DataFrame(data_list, columns=["Tarih", "Dosya Adı", "Ürün Adı", "İçerik"]), raw_logs
        except Exception as e:
            print(f"Log okuma hatası: {e}")
            return pd.DataFrame(), []

    return pd.DataFrame(), []

def delete_selected_history_items(selected_indices, current_view_data, history_type):
    """
    Hem Arama Geçmişi hem de Sınıflandırma Geçmişi için ortak silme fonksiyonu.
    Veritabanı (Emsaller) silinemez (Güvenlik için).
    """
    target_file = None
    
    # Hangi dosyayı sileceğimize karar verelim
    if history_type == "Arama Geçmişi":
        target_file = SEARCH_LOG_FILE
    elif history_type == "Sınıflandırma Geçmişi":
        target_file = CLASSIFICATION_LOG_FILE
    else:
        # "Kaydedilen Emsaller" veya tanımsız türler silinmez, görünümü olduğu gibi döndür
        return get_filtered_history(history_type=history_type)

    if not selected_indices or not os.path.exists(target_file):
        return get_filtered_history(history_type=history_type)
    
    # Silineceklerin Tarihlerini (Timestamp) alalım (Çünkü her satırda timestamp unique kabul ediyoruz)
    timestamps_to_delete = set()
    try:
        for idx in selected_indices:
            # Tablodaki 0. kolonun Tarih olduğunu varsayıyoruz
            timestamps_to_delete.add(current_view_data[idx][0]) 
    except Exception as e:
        print(f"Silme indeksi hatası: {e}")
        return get_filtered_history(history_type=history_type)
    
    # Dosyayı oku ve silinecekleri filtrele
    lines_to_keep = []
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip(): continue
                try:
                    record = json.loads(line)
                    # Eğer kaydın tarihi silinecekler listesinde YOKSA, tutuyoruz
                    if record.get("timestamp") not in timestamps_to_delete:
                        lines_to_keep.append(line)
                except: continue
        
        # Dosyayı yeniden yaz
        with open(target_file, 'w', encoding='utf-8') as f:
            f.writelines(lines_to_keep)
            
    except Exception as e:
        print(f"Dosya yazma hatası: {e}")

    # Güncel listeyi döndür
    return get_filtered_history(history_type=history_type)

def clear_all_search_history():
    if os.path.exists(SEARCH_LOG_FILE):
        try: os.remove(SEARCH_LOG_FILE)
        except: pass
    return get_filtered_history()


async def analyze_single_sds(file_path, ref_data):
    """
    Tek bir SDS dosyasını analiz eder. (Helper Function)
    """
    f_name = os.path.basename(file_path)
    
    # Regex ile ID yakalama
    product_id_match = re.search(r'^([A-Z0-9-]+)', f_name)
    product_id = product_id_match.group(0) if product_id_match else "-"

    try:
        # --- HIZ OPTİMİZASYONU: DPI Düşürme ---
        # Global fonksiyon yerine burada özel bir convert işlemi yapabiliriz veya
        # load_file_as_image fonksiyonunun DPI ayarını düşürebilirsin.
        # Hız için burada tekrar convert_from_path çağırıyorum ama düşük DPI ile.
        img = None
        if file_path.lower().endswith(".pdf"):
            # Poppler yolunu global değişkenden veya sistemden al
            poppler_path = None
            if getattr(sys, 'frozen', False):
                poppler_path = os.path.join(sys._MEIPASS, "poppler_bin")
            else:
                poppler_path = os.path.join(BASE_DIR, "poppler", "Library", "bin")
                if not os.path.exists(poppler_path): poppler_path = None

            # DPI=150 okuma hızı için idealdir
            pages = convert_from_path(file_path, dpi=150, first_page=1, last_page=1, poppler_path=poppler_path)
            if pages: img = pages[0]
        else:
            img = Image.open(file_path)

        if not img: raise Exception("Görsel okunamadı")

        # Gemini Analizi
        prompt = """
        GÖREV: Bu SDS belgesini analiz et ve aşağıdaki JSON formatını doldur.
        Özellikle Bölüm 3 (Composition) kısmındaki CAS numaralarına ve ana kimyasal isme odaklan.
        
        {
            "product_name": "Ürün Ticari Adı",
            "main_cas": "Ana bileşenin CAS numarası (yoksa null)",
            "content_summary": "İçerik özeti (Örn: %60 Solvent Naphtha)"
        }
        """
        # API isteği
        response = await llm_model.generate_content_async([prompt, img])
        json_str = response.text.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\{.*\}', json_str, re.DOTALL)
        
        ai_data = json.loads(match.group(0)) if match else {}
        
        p_name = ai_data.get("product_name", "Bulunamadı")
        cas_no = ai_data.get("main_cas", "")
        
        # Vergi Listesinde Ara
        tax_record = search_tax_db_smart(cas_no, p_name)
        
        # Rapor Satırı
        row = {
            "G.T.İ.P. *": tax_record.get("gtp", "-") if tax_record else "Eşleşme Yok",
            "İthalat Kodu": "", 
            " ": "", 
            "HAMMADDE ADI": p_name,
            "KAYIT NO": "", 
            "EK V NOTLAR": tax_record.get("tanim", "-") if tax_record else "Vergi listesinde uygun kayıt bulunamadı.",
            "CAS NR (REF:SDS)": cas_no,
            "KABUL KOŞULU": f"Vergi Oranı: %{tax_record.get('gv_oran', '?')}" if tax_record else "-",
            "GÖZDEN GEÇİRME TARİHİ ***": check_tax_date_warning(tax_record.get("gecerlilik")) if tax_record else "-",
            "NOT": f"Dosya: {f_name} | ID: {product_id}"
        }
        
        match_icon = "✅" if tax_record else "⚠️"
        log_html = f"<div>{match_icon} <b>{p_name}</b> ({cas_no}) -> {row['G.T.İ.P. *']}</div>"
        
        return row, log_html

    except Exception as e:
        err_row = {
            "G.T.İ.P. *": "HATA",
            "HAMMADDE ADI": f_name,
            "NOT": str(e)
        }
        return err_row, f"<div style='color:red'>❌ {f_name}: {e}</div>"

async def process_tax_analysis(sds_files, reference_excel):
    """
    2. ADIM (PARALEL): SDS'leri eşzamanlı analiz eder.
    """
    global llm_model
    if not llm_model: return "Model hatası.", None
    if not sds_files: return "Lütfen SDS dosyalarını yükleyin.", None

    # Referans Excel varsa oku
    ref_data = {}
    if reference_excel:
        try:
            df_ref = pd.read_excel(reference_excel.name, dtype=str)
        except: pass

    status_log = "<h3>📊 Analiz Durumu (Paralel İşlem Başlatıldı...)</h3>"
    report_data = []

    # --- PARALEL İŞLEM BAŞLANGICI ---
    tasks = []
    
    # Tüm dosyalar için görev oluştur (Henüz çalıştırma, sadece planla)
    for file_path in sds_files:
        tasks.append(analyze_single_sds(file_path, ref_data))
    
    # Hepsini aynı anda ateşle!
    # asyncio.gather tüm görevlerin bitmesini bekler ama hepsi aynı anda çalışır.
    results = await asyncio.gather(*tasks)
    
    # Sonuçları topla
    for row_data, log_msg in results:
        report_data.append(row_data)
        status_log += log_msg

    # Excel Dosyası Oluştur
    # --- DEĞİŞİKLİK: Okunabilir Tarih/Saat Formatı ---
    tarih_saat = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_filename = f"Vergi_Analiz_Raporu_{tarih_saat}.xlsx"
    # ------------------------------------------------
    
    output_path = os.path.join(BASE_DIR, output_filename)
    
    if report_data:
        df_out = pd.DataFrame(report_data)
        df_out.to_excel(output_path, index=False)
        
        # Son bir özet ekle
        total_time = datetime.now().strftime("%H:%M:%S")
        status_log += f"<br><hr><b>✅ İşlem Tamamlandı: {total_time}</b>"
        
        return status_log, output_path
    else:
        return status_log + "<br>Veri oluşmadı.", None
    

# --- 5. YENİ: TOPLU (BATCH) İŞLEM VE VERİTABANI LİSTELEME ---
# --- YARDIMCI FONKSİYON: TEK BİR DOSYAYI İŞLER ---
def process_single_file(file_obj, file_index):
    """
    Tek bir dosya için LLM isteği atar, JSON parse eder ve veriyi DÖNDÜRÜR.
    NOT: Bu fonksiyon dosyaya yazma yapmaz, sadece veriyi hazırlar.
    """
    try:
        # Dosya yolunu güvenli alma
        try:
            current_file_path = file_obj.name
        except AttributeError:
            current_file_path = str(file_obj)
            
        filename_display = os.path.basename(current_file_path)
        
        # 1. Resmi Yükle (Senin kodunda tanımlı olduğunu varsayıyorum)
        image_file = load_file_as_image(current_file_path)
        if image_file is None:
            return {"status": "error", "msg": "Resim yüklenemedi", "file": filename_display}

        # --- JSON ŞABLONU ---
        example_json_structure = """
        {
            "product_name": "ÜRÜN TİCARİ ADI",
            "brand": "MARKA (Yoksa boş string)",
            "assigned_gtip": "XXXX.XX.XX.XX.XX",
            "assigned_by": "consultant",
            "assignment_date": "YYYY-MM-DD",
            "source_type": "pdf_image", 
            "composition_text": "Ürünün kimyasal içeriği, CAS no, oranlar vb.",
            "features": {
                "use": "Kullanım alanı (örn: sertleştirici, boya hammaddesi)",
                "form": "liquid/powder/solid",
                "nonvolatile_pct": null,
                "solvent_present": false,
                "polymer_family": null,
                "is_surfactant": false,
                "is_primary_polymer_form": false,
                "is_paint_or_varnish": false,
                "ionicity": "null"
            },
            "tags": ["etiket1", "etiket2"],
            "short_reason": "Neden bu GTIP seçildiğine dair kısa teknik açıklama.",
            "verified": false,
            "quality": "ok"
        }
        """

        prompt = f"""
        GÖREV: Ekteki gümrük sınıflandırma formunu (GTIP TESPİT FORMU) uzman bir kimya mühendisi gibi analiz et.
        
        KURALLAR:
        1. "assignment_date" alanına belgedeki tarihi YYYY-MM-DD formatında yaz.
        2. "assigned_gtip" belgede yazan GTIP kodudur.
        3. "features" altındaki alanları kimyasal bilginle doldur.
        4. "short_reason" kısmına Türkçe, net bir gerekçe yaz.
        5. "product_name" belgedeki en belirgin ürün adıdır.
        6. SADECE JSON döndür. Yorum veya markdown ekleme.

        İSTENEN JSON FORMATI:
        {example_json_structure}
        """

        # 2. Model İsteği
        if not llm_model:
            return {"status": "error", "msg": "Model yüklü değil", "file": filename_display}
            
        response = llm_model.generate_content([prompt, image_file])
        
        # 3. JSON Temizliği
        json_str = response.text.replace("```json", "").replace("```", "").strip()
        match = re.search(r'\{.*\}', json_str, re.DOTALL)
        
        if match:
            data = json.loads(match.group(0))
            
            # Post-processing (Eksik alanları doldurma)
            data["id"] = f"auto_{int(time.time())}_{file_index}"
            data["source_path"] = filename_display
            
            if not data.get("assignment_date"):
                data["assignment_date"] = datetime.now().strftime("%Y-%m-%d")
                
            data["version_date"] = datetime.now().strftime("%Y-%m-%d")
            
            return {"status": "success", "data": data, "file": filename_display}
        else:
            return {"status": "error", "msg": "JSON parse edilemedi", "file": filename_display}

    except Exception as e:
        return {"status": "error", "msg": str(e), "file": filename_display}


# --- ANA FONKSİYON: PARALEL İŞLEME VE GÜVENLİ YAZMA ---
def process_batch_files(file_paths, progress=gr.Progress()):
    global llm_model
    if not llm_model: return "Model hazır değil, API anahtarını kontrol edin.", ""
    if not file_paths: return "Lütfen dosya seçin.", ""

    if not isinstance(file_paths, list):
        file_paths = [file_paths]

    total_files = len(file_paths)
    print(f"--- Toplu İşlem Başlatıldı: {total_files} Dosya (Paralel + Kilitli Yazma) ---")

    html_report = "<h3>🚀 İşlem Raporu</h3>"
    cards_html = ""
    
    # --- THREAD POOL BAŞLANGICI ---
    # max_workers=5: Aynı anda 5 dosya işler.
    with ThreadPoolExecutor(max_workers=5) as executor:
        # Görevleri dağıt
        future_to_file = {executor.submit(process_single_file, f, i): i for i, f in enumerate(file_paths)}
        
        completed_count = 0
        
        # Görevler bittikçe sonuçları al
        for future in as_completed(future_to_file):
            completed_count += 1
            progress((completed_count / total_files), desc=f"İşleniyor {completed_count}/{total_files}...")
            
            res = future.result()
            
            status_icon = "❓"
            status_msg = ""
            
            if res["status"] == "success":
                new_case_data = res["data"]
                status_icon = "✅"
                status_msg = "Başarılı"
                p_name = new_case_data.get('product_name', 'Bilinmiyor')
                
                print(f"-> İşlendi: {p_name}")

                # --- KRİTİK BÖLÜM: DOSYAYA GÜVENLİ YAZMA ---
                try:
                    # KİLİT (LOCK) İLE YAZMA: Başka thread yazarken bekler
                    with file_writer_lock:
                        with open(CASES_FILE, 'a', encoding='utf-8') as f:
                            json_line = json.dumps(new_case_data, ensure_ascii=False)
                            f.write(json_line + "\n")
                            f.flush()            # Python tamponunu boşalt
                            os.fsync(f.fileno()) # Diske yazmayı zorla (RunPod için şart)
                    
                    print(f"   💾 DİSKE YAZILDI: {p_name}") # Logda bunu görmelisin
                    
                except Exception as e:
                    print(f"!!! KRİTİK YAZMA HATASI: {e}")
                    status_msg = f"Yazma Hatası: {str(e)}"
                    status_icon = "💾"

                # HTML KART OLUŞTURMA
                gtip = new_case_data.get('assigned_gtip', '-')
                reason = new_case_data.get('short_reason', '-')
                use_area = new_case_data.get('features', {}).get('use', 'Belirtilmemiş')

                cards_html += f"""
                <div style="font-family:sans-serif; border:1px solid #ddd; border-radius:8px; margin-bottom:15px; background:white; box-shadow:0 2px 4px rgba(0,0,0,0.05); overflow:hidden;">
                    <div style="background:#E3F2FD; padding:10px 15px; border-bottom:1px solid #BBDEFB; display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-weight:bold; color:#1565C0;">{p_name}</span>
                        <span style="background:#1565C0; color:white; padding:2px 8px; border-radius:4px; font-size:0.9em;">{gtip}</span>
                    </div>
                    <div style="padding:15px;">
                        <div style="font-size:0.85em; color:#999; margin-bottom:5px;">
                            📅 {new_case_data.get('assignment_date')} | 🧪 {use_area}
                        </div>
                        <div style="background:#f9f9f9; padding:8px; border-left:3px solid #FF9800; font-style:italic; color:#666; font-size:0.9em;">
                            "{reason}"
                        </div>
                    </div>
                </div>
                """
            else:
                status_icon = "❌"
                status_msg = res.get("msg", "Hata")
                print(f"-> HATA: {res['file']} - {status_msg}")

            # Rapor satırı
            html_report += f"""
            <div style="border-bottom:1px solid #eee; padding:8px; display:flex; justify-content:space-between;">
                <span>{status_icon} <b>{res['file']}</b></span>
                <span style="color:#666; font-size:0.9em;">{status_msg[:30]}</span>
            </div>
            """

    return html_report, cards_html


def get_all_cases_as_df():
    """
    YENİ: Veritabanındaki (cases.jsonl) tüm kayıtları tablo olarak döndürür.
    """
    if not os.path.exists(CASES_FILE):
        return pd.DataFrame(columns=["Durum"]), "Veritabanı dosyası henüz oluşmamış."
    
    data = []
    try:
        with open(CASES_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                if not line.strip(): continue
                try:
                    j = json.loads(line)
                    data.append([
                        j.get("product_name"),
                        j.get("assigned_gtip"),
                        j.get("assignment_date"),
                        j.get("short_reason")
                    ])
                except: pass
        
        df = pd.DataFrame(data, columns=["Ürün Adı", "GTIP", "Tarih", "Gerekçe"])
        return df, f"Toplam {len(df)} kayıt listelendi."
    except Exception as e:
        return pd.DataFrame(), f"Hata: {e}"

# --- 6. ARAMA MOTORU (ORİJİNAL MANTIK KORUNDU) --- 
def search_jsonl_directly(query, limit=5):
    if not os.path.exists(CASES_FILE):
        return [], "Veri dosyası (cases.jsonl) bulunamadı."

    def normalize(text):
        return re.sub(r'[\W_]+', '', str(text).lower())

    results = []
    query_raw = query.lower().strip()
    query_norm = normalize(query) 
    query_terms = query_raw.split() 

    try:
        with open(CASES_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for line in lines:
            if not line.strip(): continue
            try:
                case = json.loads(line)
                score = 0
                
                p_name = case.get('product_name', '')
                p_name_lower = p_name.lower()
                p_name_norm = normalize(p_name)
                gtip = str(case.get('assigned_gtip', ''))
                gtip_norm = normalize(gtip)
                comp = case.get('composition_text', '').lower()

                # Puanlama Algoritması (Orijinal)
                if query_norm and (query_norm in p_name_norm or p_name_norm in query_norm):
                    score += 40
                if query_norm and (query_norm in gtip_norm):
                    score += 50
                for term in query_terms:
                    if term in p_name_lower: score += 15
                    elif term in comp: score += 5
                    elif normalize(term) in p_name_norm: score += 10

                similarity = SequenceMatcher(None, query_raw, p_name_lower).ratio()
                if similarity > 0.6: score += int(similarity * 20)

                if score > 0: results.append((score, case))
            except: continue
        
        results.sort(key=lambda x: x[0], reverse=True)
        top_cases = [r[1] for r in results[:int(limit)]]
        
        if not top_cases: return [], "Eşleşme bulunamadı."
        return top_cases, f"{len(results)} kayıt bulundu, en alakalı {len(top_cases)} gösteriliyor."

    except Exception as e:
        return [], f"Arama hatası: {e}"

async def extract_keywords_from_image(image):
    global llm_model
    if not llm_model: return "Model hatası."
    if not image: return ""

    prompt = """
    GÖREV: Bu görsel bir kimyasal ürünün etiketi veya SDS sayfasıdır.
    AMAÇ: Bu ürünü veritabanında aratmak için en önemli anahtar kelimeleri çıkar.
    
    YAPILACAKLAR:
    1. Ürün Ticari Adını bul.
    2. Ana bileşenleri (kimyasal isimler veya CAS no) bul.
    3. Gereksiz kelimeleri (LTD, ŞTİ, Adres vb.) at.
    4. Sonuç olarak sadece yan yana yazılmış arama terimleri döndür.
    
    ÖRNEK ÇIKTI:
    Rheobyk-431 Polyamide iso-butanol
    """
    
    try:
        response = await llm_model.generate_content_async([prompt, image])
        return response.text.strip()
    except Exception as e:
        return f"Hata: {str(e)}"

# --- GÜNCELLENMİŞ ASİSTAN FONKSİYONU ---
async def classify_batch_with_metadata(files, metadata_df, pasted_image_path):
    """
    GÜNCELLENDİ (V6 - TABLO ÖNCELİKLİ & HİBRİT):
    - Kullanıcının tabloda yaptığı isim değişikliklerini (Rename) esas alır.
    - Dosya/Resim sırası ile Tablo satır sırasını eşleştirir (Index Matching).
    - Hem toplu dosyaları hem de yapıştırılan tekil görseli işler.
    """
    global llm_model
    if not llm_model: return "Model hatası."
    
    # 1. İşlenecek Kaynakları Sırayla Listele (Sıra Önemli: Önce Dosyalar, Sonra Paste)
    # Bu sıralama create_metadata_table fonksiyonundaki sıralamayla AYNI olmalı.
    resource_paths = []
    
    if files:
        for f in files:
            resource_paths.append(f.name)
            
    if pasted_image_path:
        resource_paths.append(pasted_image_path)

    if not resource_paths: return "Lütfen en az bir dosya yükleyin veya görsel yapıştırın."

    # 2. Metadata Tablosunu Oku
    meta_rows = []
    if metadata_df is not None:
        if isinstance(metadata_df, pd.DataFrame):
            meta_rows = metadata_df.fillna("").values.tolist()
        elif isinstance(metadata_df, list):
            meta_rows = metadata_df
            
    # Eğer tablo boş geldiyse (çok nadir), boş satırlarla doldur
    if not meta_rows:
        meta_rows = [["", "", "", ""]] * len(resource_paths)

    final_report = "<h3>🧠 Detaylı Sınıflandırma Raporu</h3>"

    # --- ANA DÖNGÜ (SIRALI EŞLEŞTİRME) ---
    # Kaynak dosyalar ile tablodaki satırları sırasıyla (zip) eşleştiriyoruz.
    for i, f_path in enumerate(resource_paths):
        
        # O anki dosya için tablodaki veriyi çek
        # Eğer tablo satır sayısı dosya sayısından azsa (hata toleransı), varsayılan değer kullan
        if i < len(meta_rows):
            row = meta_rows[i]
            # Tablodaki 1. Sütun (Dosya Adı) - Kullanıcı değiştirdiyse bunu alacağız!
            display_filename = str(row[0]) if row[0] else os.path.basename(f_path)
            # Tablodaki 2. Sütun (Ürün Adı)
            p_name = str(row[1]) if len(row) > 1 and row[1] else ""
            comp = str(row[2]) if len(row) > 2 and row[2] else ""
            use = str(row[3]) if len(row) > 3 and row[3] else ""
        else:
            display_filename = os.path.basename(f_path)
            p_name, comp, use = "", "", ""

        # --- GÖRÜNÜM AYARI ---
        # Başlıkta görünecek isim: Varsa Ürün Adı, yoksa Dosya Adı
        final_header_name = p_name if p_name else display_filename

        try:
            # Görseli Yükle
            img = load_file_as_image(f_path)
            if img is None: raise Exception("Dosya formatı okunamadı.")

            # 1. RAG (Arama - Kullanıcı girdilerini dahil et)
            search_query = f"{p_name} {comp} {display_filename}"
            similar_cases, _ = search_jsonl_directly(search_query, limit=3)
            
            context_text = "SİSTEMDEKİ BENZER EMSALLER (Referans Al):\n"
            if similar_cases:
                for c in similar_cases:
                    context_text += f"- {c.get('product_name')} -> GTIP: {c.get('assigned_gtip')} ({c.get('short_reason')})\n"
            else:
                context_text += "Benzer emsal bulunamadı, mevzuat bilgini kullan.\n"

            # 2. Prompt Hazırlığı
            user_context = ""
            if p_name.strip(): user_context += f"- Ürün Ticari Adı: {p_name}\n"
            if display_filename.strip(): user_context += f"- Dosya/Etiket Adı: {display_filename}\n"
            if comp.strip(): user_context += f"- İçerik: {comp}\n"
            if use.strip(): user_context += f"- Kullanım: {use}\n"

            prompt = f"""
            ROL: Sen uzman bir Türk Gümrük Müşaviri ve Kimyagerisin.
            GÖREV: Aşağıdaki ürünü (görseli ve verilen metinleri birleştirerek) sınıflandır.
            
            KULLANICI GİRDİLERİ (Bunu Kesin Doğru Kabul Et):
            {user_context}
            
            {context_text}
            
            İSTENEN ÇIKTI FORMATI (HTML):
            <div style="font-family:sans-serif; color:#333;">
                <h4 style="color:#d35400; border-bottom:1px solid #ddd; padding-bottom:5px;">1. Ürün ve Kimyasal Analiz</h4>
                <p><strong>Ürün Tanımı:</strong> (Ürün adını "{p_name if p_name else display_filename}" olarak baz al ve tanımla.)</p>
                <p><strong>Kimyasal Yapı:</strong> (Kimyasal yapısını açıkla.)</p>
                
                <h4 style="color:#2980b9; border-bottom:1px solid #ddd; padding-bottom:5px;">2. Mevzuat ve Fasıl Yorumu</h4>
                <p>(Gümrük Tarife Cetveli yorumunu yap.)</p>
                
                <div style="background:#e8f8f5; padding:10px; border-radius:5px; margin:10px 0; border-left:5px solid #1abc9c;">
                    <strong>🎯 Önerilen GTIP:</strong> [12 Haneli Kod]
                </div>
                
                <h4 style="color:#8e44ad; border-bottom:1px solid #ddd; padding-bottom:5px;">4. Uzman Görüşü</h4>
                <p>(Varsa ek uyarılar.)</p>
            </div>
            """
            
            # Model İsteği
            response = await llm_model.generate_content_async([prompt, img])
            
            # Loglama (Geçmişe senin verdiğin isimle kaydeder)
            log_classification_to_history(display_filename, p_name, comp, response.text)

            # Rapor HTML'ine Ekle
            final_report += f"""
            <details style="background:white; border:1px solid #bdc3c7; margin-bottom:15px; padding:0; border-radius:8px; overflow:hidden;">
                <summary style="cursor:pointer; background:#ecf0f1; padding:12px 15px; font-weight:bold; color:#2c3e50; display:flex; justify-content:space-between; align-items:center;">
                    <span>📄 {final_header_name}</span>
                    <span style="font-size:0.85em; color:#7f8c8d; background:white; padding:3px 8px; border-radius:10px;">Analizi Göster ⬇️</span>
                </summary>
                <div style="padding:20px; line-height:1.6;">
                    {response.text}
                </div>
            </details>
            """

        except Exception as e:
            print(f"Hata ({display_filename}): {e}")
            final_report += f"<div style='color:white; background:#e74c3c; padding:10px; margin-bottom:10px; border-radius:5px;'>❌ <b>{display_filename}</b> hatası: {str(e)}</div>"

    return final_report
async def classify_product_smart(product_name, composition, use, image_files):
    """
    GÜNCELLENDİ: Hem tekil metin girdisi hem de ÇOKLU DOSYA (Batch) desteği.
    Eğer 'image_files' bir liste ise toplu analiz yapar, değilse tekil analiz yapar.
    """
    global llm_model
    if not llm_model: return "Model hatası. Ayarları kontrol edin."

    # --- SENARYO 1: ÇOKLU DOSYA YÜKLENMİŞSE (BATCH SDS ANALİZİ) ---
    # Gradio 'file_count="multiple"' olduğunda liste gönderir.
    if image_files and isinstance(image_files, list):
        final_report = "<h3>🧠 Toplu Sınıflandırma Raporu</h3>"
        
        for i, img_path in enumerate(image_files):
            try:
                img = Image.open(img_path)
                
                # Dosya için özel prompt (İsmi ve içeriği kendisi bulsun)
                batch_prompt = """
                GÖREV: Bu SDS/Etiket görselini analiz et.
                1. Ürün adını ve içeriğini görselden çıkar.
                2. Türk Gümrük Tarife Cetveli'ne göre sınıflandır.
                
                ÇIKTI FORMATI (HTML):
                <div style='margin-bottom:5px;'><strong>Ürün Adı:</strong> [Bulunan Ad]</div>
                <div style='margin-bottom:5px;'><strong>GTIP Önerisi:</strong> [Kod]</div>
                <div style='font-size:0.9em;'><strong>Gerekçe:</strong> [Kısa Açıklama]</div>
                <hr>
                """
                
                # Hızlı olması için RAG kullanmadan direkt görsel analizi yapıyoruz
                response = await llm_model.generate_content_async([batch_prompt, img])
                
                # Akordeon (Açılır/Kapanır) Yapısı
                final_report += f"""
                <details style="background:white; border:1px solid #ccc; margin-bottom:10px; padding:10px; border-radius:5px;">
                    <summary style="cursor:pointer; font-weight:bold; color:#2c3e50;">
                        📄 {os.path.basename(img_path)} (Tıkla & Gör)
                    </summary>
                    <div style="margin-top:10px; color:#333;">
                        {response.text}
                    </div>
                </details>
                """
            except Exception as e:
                final_report += f"<div style='color:red;'>❌ {os.path.basename(img_path)} hatası: {e}</div>"
        
        return final_report

    # --- SENARYO 2: TEKİL GİRİŞ (ESKİ MANTIK) ---
    else:
        # 1. RAG (Benzer Emsalleri Bul)
        search_text = f"{product_name} {composition}"
        similar_cases, _ = search_jsonl_directly(search_text, limit=3)
        
        context_text = "SİSTEMDEKİ BENZER EMSALLER (Referans Al):\n"
        if similar_cases:
            for c in similar_cases:
                context_text += f"- {c.get('product_name')} -> GTIP: {c.get('assigned_gtip')} ({c.get('short_reason')})\n"
        else:
            context_text += "Benzer emsal bulunamadı, sadece mevzuat bilgini kullan.\n"

        # 2. Prompt
        prompt = f"""
        ROL: Sen uzman bir Türk Gümrük Müşaviri ve Kimyagerisin.
        GÖREV: Aşağıdaki ürünü Türk Gümrük Tarife Cetveli'ne (TGTC) göre sınıflandır ve GTIP öner.

        GİRDİLER:
        - Ürün Adı: {product_name}
        - İçerik/Bileşim: {composition}
        - Kullanım Alanı: {use}
        
        {context_text}

        İSTENEN ÇIKTI FORMATI (Markdown/HTML):
        ### 1. Ürün ve Kimyasal Analiz
        (Ürünün ne olduğunu, kimyasal yapısını ve fonksiyonunu kısaca açıkla.)

        ### 2. Mevzuat ve Fasıl Yorumu
        (Bu ürün hangi Fasıl'a girer? Neden? İlgili Gümrük Tarife İzahnamesi notlarına atıfta bulun.)
        
        ### 3. Önerilen GTIP
        (En olası 12 haneli GTIP numarasını yaz.)

        ### 4. Uzman Görüşü / Uyarılar
        """
        
        inputs = [prompt]
        # Eğer image_files tek bir dosya objesi veya path ise
        if image_files and not isinstance(image_files, list):
            # Gradio bazen path string, bazen PIL objesi verir, type check yapabiliriz veya direkt açmayı deneriz
            try:
                inputs.append(Image.open(image_files))
                inputs.append("EKTEKİ GÖRSELİ (SDS/ETİKET) DETAYLICA OKU VE İÇERİK BİLGİSİ OLARAK KULLAN.")
            except:
                pass # Resim açılamazsa metinle devam et
        
        try:
            response = await llm_model.generate_content_async(inputs)
            return response.text
        except Exception as e:
            return f"Hata oluştu: {str(e)}"

async def search_and_explain(query, limit, image_for_log=None):
    global llm_model
    if not query: return "Lütfen arama terimi girin."
    
    cases, msg = search_jsonl_directly(query, int(limit))
    
    # Geçmişe Kaydet
    log_search_to_history(query, cases, image_for_log)
    
    if not cases: return f"Sonuç bulunamadı. ({msg})"
    
    html_out = f"<div style='margin-bottom:10px; color:green;'>ℹ️ {msg}</div>"
    
    # AI Yorumları (Paralel/Hızlı olması için basit prompt)
    ai_comments = {}
    if llm_model:
        try:
            summary_for_ai = []
            for idx, c in enumerate(cases):
                summary_for_ai.append({"id": idx, "urun": c.get('product_name'), "icerik": c.get('composition_text')[:100]})
            
            prompt = f"KULLANICI: {query}. KAYITLAR: {json.dumps(summary_for_ai)}. Her biri için tek cümlelik ilişki yorumu yap. JSON Çıktı: [{{'id':0, 'yorum':'...'}}]"
            resp = await llm_model.generate_content_async(prompt)
            clean = resp.text.replace("```json","").replace("```","").strip()
            match = re.search(r'\[.*\]', clean, re.DOTALL)
            if match:
                for item in json.loads(match.group(0)): ai_comments[item['id']] = item['yorum']
        except: pass

    for idx, case in enumerate(cases):
        comment = ai_comments.get(idx, "Eşleşme bulundu.")
        date_info = case.get('assignment_date', case.get('date', '-'))

        html_out += f"""
        <div style="border:1px solid #ccc; padding:15px; margin-bottom:15px; border-radius:8px; background:white; box-shadow: 0 2px 5px rgba(0,0,0,0.05);">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #eee; padding-bottom:8px;">
                <strong style="color:#1565C0; font-size:1.1em;">{case.get('product_name', 'İsimsiz Ürün')}</strong>
                <span style="background:#E3F2FD; color:#0D47A1; padding:4px 8px; border-radius:4px; font-weight:bold; font-size:0.9em;">{case.get('assigned_gtip', '-')}</span>
            </div>
            <div style="margin-top:10px; font-size:0.95em; color:#333;">
                <strong>İçerik:</strong> {case.get('composition_text', '-')}
            </div>
            <div style="margin-top:5px; font-size:0.95em; color:#555;">
                <strong>Kullanım:</strong> {case.get('features', {}).get('use', '-')}
            </div>
            <div style="margin-top:12px; background:#FFF3E0; padding:10px; border-radius:6px; font-size:0.95em; color:#E65100; border:1px solid #FFE0B2;">
                🤖 <strong>AI Analizi:</strong> {comment}
            </div>
            <div style="margin-top:8px; text-align:right;">
                <span style="font-size:0.8em; color:#888; background:#f5f5f5; padding:3px 8px; border-radius:12px;">
                    📅 Tarih: {date_info}
                </span>
            </div>
        </div>
        """
    return html_out


# --- VERGİ ASİSTANI İÇİN YARDIMCI FONKSİYONLAR ---

TAX_DB_FILE = os.path.join(BASE_DIR, "vergi_listesi.jsonl")
TAX_META_FILE = os.path.join(BASE_DIR, "vergi_meta.json")

def get_tax_db_status():
    """Sisteme en son ne zaman vergi listesi yüklendiğini kontrol eder."""
    if os.path.exists(TAX_META_FILE):
        try:
            with open(TAX_META_FILE, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            return f"✅ Mevcut Liste: {meta.get('filename')} (Yükleme: {meta.get('upload_date')})"
        except:
            return "⚠️ Veri dosyası bozuk."
    return "❌ Henüz bir vergi listesi yüklenmedi."

def process_and_save_tax_excel(file_obj):
    """
    Yüklenen Excel (V Sayılı Liste) dosyasını işler ve JSONL formatına çevirip kaydeder.
    GÜNCELLENDİ: İşlem sonunda anlık durumu (get_tax_db_status) döndürür.
    """
    if file_obj is None:
        return "Lütfen bir Excel dosyası yükleyin."

    try:
        # 1. Dosyayı önce başlıksız ham olarak oku
        df_raw = pd.read_excel(file_obj.name, header=None, dtype=str)
        
        # 2. "GTP" kelimesinin geçtiği satırı bul (Header Detection)
        header_row_index = -1
        for i, row in df_raw.iterrows():
            # Satırdaki tüm değerleri string yapıp birleştirip içinde GTP var mı bak
            row_text = " ".join([str(x).upper() for x in row.values])
            if "GTP" in row_text and "EŞYA TANIMI" in row_text:
                header_row_index = i
                break
        
        if header_row_index == -1:
            return "HATA: Excel dosyasında 'GTP' ve 'EŞYA TANIMI' başlıkları bulunamadı. Lütfen dosyayı kontrol edin."

        # 3. Bulunan satırı başlık (header) kabul ederek yeniden oku
        df = pd.read_excel(file_obj.name, header=header_row_index, dtype=str)
        
        # Kolon isimlerini temizle (Boşlukları at, büyük harf yap, yeni satırları sil)
        df.columns = df.columns.str.strip().str.upper().str.replace('\n', '')
        
        # Kritik kolonları tekrar kontrol et
        required_cols = ["GTP", "EŞYA TANIMI"]
        missing = [col for col in required_cols if col not in df.columns]
        
        if missing:
            return f"HATA: Başlık satırı bulundu ama şu kolonlar eksik: {missing}"

        processed_count = 0
        records = []

        for _, row in df.iterrows():
            # GTP veya Tanım boşsa o satırı atla
            gtp_raw = str(row.get("GTP", "")).strip()
            desc_raw = str(row.get("EŞYA TANIMI", "")).strip()
            
            if not gtp_raw or not desc_raw or gtp_raw.lower() == "nan":
                continue

            # Bazen GTP hücresinde birden fazla numara alt alta yazılır (Örn: "2710.19.81\n2710.19.99")
            # Bunları tek tek ayırıp ayrı kayıtlar oluşturacağız ki arama kolay olsun.
            gtp_list = gtp_raw.replace('\n', ' ').replace('\r', ' ').split() 
            
            for gtp_code in gtp_list:
                # Temiz kayıt objesi
                # GV (%) kolonu bazen "GV" bazen "GV (%)" olabilir, esnek alalım
                gv_col = "GV (%)" if "GV (%)" in df.columns else "GV"
                
                record = {
                    "gtp": gtp_code.strip(),
                    "tanim": desc_raw,
                    "gv_oran": str(row.get(gv_col, "0")).strip(),
                    "dipnot": str(row.get("DİPNOT", "")).strip(),
                    # Gözden geçirme tarihi bazen farklı isimle gelebilir, opsiyonel yapalım
                    "gecerlilik": str(row.get("GÖZDEN GEÇİRME TARİHİ**", row.get("GÖZDEN GEÇİRME TARİHİ", "-"))).strip()
                }
                records.append(record)
                processed_count += 1

        # JSONL Olarak Kaydet (Eski dosyanın üzerine yazar)
        with open(TAX_DB_FILE, 'w', encoding='utf-8') as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # Meta veriyi kaydet (Tarih ve Dosya Adı)
        meta_info = {
            "filename": os.path.basename(file_obj.name),
            "upload_date": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "total_records": processed_count
        }
        with open(TAX_META_FILE, 'w', encoding='utf-8') as f:
            json.dump(meta_info, f, ensure_ascii=False)

        # --- KRİTİK NOKTA DÜZELTİLDİ ---
        # Dosyayı yazdıktan hemen sonra okumaya çalıştığımızda bazen eski veriyi getirebiliyor.
        # Bu yüzden tekrar okumak yerine, elimizdeki güncel 'meta_info' verisini kullanıyoruz.
        return f"✅ Mevcut Liste: {meta_info['filename']} (Yükleme: {meta_info['upload_date']})"

    except Exception as e:
        return f"❌ İşlem sırasında hata oluştu: {str(e)}"


# --- 7. GRADIO ARAYÜZÜ (BAŞLATMA) ---
load_config()
initialize_gemini_model()
fastapi_app = fastapi.FastAPI()

with gr.Blocks(theme=gr.themes.Monochrome(), title="GTIP Uzmanı") as gradio_ui:
    gr.Markdown("# 🇹🇷 GTIP Sınıflandırma & Emsal Yönetim Sistemi ")
    
    with gr.Tabs():

        # === SEKME 1: EMSAL ARAMA ===
        with gr.TabItem("Emsal Arama"):
            gr.Markdown("### 🔍 Veritabanında Arama")
            with gr.Accordion("📸 Fotoğraf ile Otomatik Doldur (SDS / Etiket)", open=False):
                with gr.Row():
                    with gr.Column(scale=3):
                        search_image_input = gr.Image(label="Fotoğrafı Buraya Sürükleyin", type="pil", height=150)
                    with gr.Column(scale=1):
                        gr.Markdown("<br>")
                        img_to_text_btn = gr.Button("Fotoğrafı Oku ve\nArama Kutusuna Yaz ⬇️", variant="secondary")
            
            with gr.Row():
                search_input = gr.Textbox(label="Arama Terimi", placeholder="Örn: RHEOBYK, 3208, Polyamid...", scale=4)
                limit_slider = gr.Slider(1, 20, value=5, step=1, label="Adet", scale=1)
                search_btn = gr.Button("Ara", variant="primary", scale=1)
            
            search_output = gr.HTML(label="Sonuçlar")

            img_to_text_btn.click(extract_keywords_from_image, inputs=[search_image_input], outputs=[search_input])
            search_btn.click(search_and_explain, inputs=[search_input, limit_slider, search_image_input], outputs=[search_output])

        # === SEKME 2: YENİ EMSAL EKLE (GÜNCELLENDİ: TOPLU/QUEUE) ===
        with gr.TabItem("Yeni Emsal Ekle"):
            gr.Markdown("### 📸 Fotoğraftan Veri Çıkar ve Kaydet")
            gr.Markdown("SDS veya GTIP Formlarını yükleyin. Sistem sırayla (Queue) işleyip veritabanına ekleyecektir.")

            with gr.Accordion("📂 Veritabanındaki Tüm Emsalleri Listele", open=False):
                refresh_db_btn = gr.Button("🔄 Listeyi Yenile", size="sm")
                db_status_txt = gr.Label(show_label=False)
                db_table = gr.Dataframe(interactive=False, wrap=True, headers=["Ürün Adı", "GTIP", "Tarih", "Gerekçe"])
                refresh_db_btn.click(get_all_cases_as_df, outputs=[db_table, db_status_txt])

            gr.Markdown("---")
            
            with gr.Row():
                with gr.Column(scale=1):
                    # ÇOKLU DOSYA SEÇİMİ
                    files_input = gr.File(label="Dosyaları Seçin (Çoklu Seçim)", file_count="multiple", type="filepath")
                    batch_process_btn = gr.Button("🚀 Toplu Analiz ve Kayıt Başlat", variant="primary")
                
                with gr.Column(scale=1):
                    # ÇIKTILAR ARTIK HTML
                    batch_report_output = gr.HTML(label="İşlem Raporu")
                    cards_preview_output = gr.HTML(label="Eklenen Kartlar") # <-- BURASI HTML OLDU

            batch_process_btn.click(
                fn=process_batch_files,
                inputs=[files_input],
                outputs=[batch_report_output, cards_preview_output]
            )

        # === SEKME 3: ASİSTAN ===
        with gr.TabItem("Sınıflandırma Asistanı"):
            gr.Markdown("### 🧠 Detaylı Sınıflandırma Asistanı")
            gr.Markdown("İster tek bir ekran görüntüsü yapıştırın, ister birden fazla PDF/Resim yükleyin.")
            
            with gr.Row():
                # SOL SÜTUN: GİRDİLER
                with gr.Column(scale=4):
                    
                    with gr.Group():
                        with gr.Row():
                            # 1. Alan: Hızlı Yapıştır
                            cls_paste_input = gr.Image(
                                label="📋 Hızlı Yapıştır (Ctrl+V)", 
                                type="filepath", 
                                sources=["clipboard"], # Sadece yapıştırma açık
                                height=150
                            )
                            # 2. Alan: Çoklu Dosya
                            cls_files = gr.File(
                                label="📂 Dosyaları Seç (Çoklu PDF/Resim)", 
                                file_count="multiple", 
                                type="filepath",
                                height=150
                            )

                    # 3. Metaveri Tablosu
                    gr.Markdown("##### 📝 Ürün Bilgileri (Dosya yüklerseniz otomatik satır açılır)")
                    cls_table = gr.Dataframe(
                        headers=["Dosya Adı", "Ürün Adı", "İçerik / Bileşim", "Kullanım Alanı"],
                        datatype=["str", "str", "str", "str"],
                        col_count=(4, "fixed"),
                        interactive=True,
                        label="Ürün Detay Tablosu"
                    )
                    
                    # Dosya yüklenince Tabloyu Dolduracak Event (Sadece cls_files için çalışır)
                    # 1. Dosya yüklenince tabloyu güncelle (Girdi olarak hem dosyayı hem paste'i alır)
                    cls_files.change(
                        fn=create_metadata_table, 
                        inputs=[cls_files, cls_paste_input], 
                        outputs=cls_table
                    )
                    
                    # 2. Resim yapıştırılınca da tabloyu güncelle (ÖNEMLİ OLAN BU)
                    cls_paste_input.change(
                        fn=create_metadata_table, 
                        inputs=[cls_files, cls_paste_input], 
                        outputs=cls_table
                    )                    

                    # Buton
                    cls_btn = gr.Button("Analizi Başlat ✨", variant="primary")
                
                # SAĞ SÜTUN: ÇIKTI
                with gr.Column(scale=5):
                    cls_output = gr.HTML(label="Asistan Raporu")
            
            # Buton Aksiyonu: Hem dosyaları hem yapıştırılan resmi gönderiyoruz
            cls_btn.click(
                fn=classify_batch_with_metadata, 
                inputs=[cls_files, cls_table, cls_paste_input], # <-- Yeni input eklendi
                outputs=[cls_output]
            )


        # === SEKME 4: AYARLAR ===
        with gr.TabItem("Ayarlar"):
            gr.Markdown("### ⚙️ Yapılandırma")
            with gr.Column():
                api_in = gr.Textbox(label="Google Gemini API Key", value=mask_api_key(app_config["api_key"]), type="password")
                check_btn = gr.Button("🔑 Anahtarı Doğrula ve Modelleri Listele", variant="secondary")
                model_dropdown = gr.Dropdown(label="Kullanılacak Model", choices=[app_config["model_name"]], value=app_config["model_name"], allow_custom_value=True)
                save_settings_btn = gr.Button("💾 Ayarları Kaydet", variant="primary")
                settings_status = gr.Label(label="Durum", value="Bekleniyor...")

            check_btn.click(list_available_models, inputs=[api_in], outputs=[model_dropdown, settings_status])
            
            def save_full_settings(key_input, model_selection):
                if "..." in key_input: final_key = app_config.get("api_key")
                else: final_key = key_input
                if save_config(final_key, model_selection):
                    initialize_gemini_model()
                    return f"✅ Ayarlar kaydedildi! Model: {model_selection}"
                else: return "❌ Hata."

            save_settings_btn.click(save_full_settings, inputs=[api_in, model_dropdown], outputs=[settings_status])

        # === SEKME 5: GEÇMİŞ (GÜNCELLENDİ: BİRLEŞİK GÖRÜNÜM) ===
        with gr.TabItem("Geçmiş"):
            gr.Markdown("### 🗂️ Veri Yönetimi")
            with gr.Row():
                # YENİ: RADIO BUTTON İLE SEÇİM
                hist_type_selector = gr.Radio(
                    choices=["Arama Geçmişi", "Kaydedilen Emsaller", "Sınıflandırma Geçmişi"], 
                    value="Arama Geçmişi", 
                    label="Görüntüleme Modu"
                )
                hist_filter = gr.Textbox(label="Filtrele", placeholder="Terim girin...", scale=2)
                hist_refresh = gr.Button("🔄 Yenile", scale=1)
                hist_del_sel = gr.Button("🗑️ Seçileni Sil", variant="secondary", scale=1)
                hist_del_all = gr.Button("⚠️ Tümünü Temizle", variant="stop", scale=1)

            with gr.Row():
                with gr.Column(scale=3):
                    hist_table = gr.Dataframe(interactive=False, wrap=True)
                with gr.Column(scale=2):
                    gr.Markdown("### Detay")
                    det_img = gr.Image(label="Görsel", height=200, interactive=False, visible=False)
                    det_html = gr.HTML(label="Detay Verisi") # JSON yerine HTML de kullanabiliriz veya JSON

            hist_raw = gr.State([])
            hist_view = gr.State([]) 
            sel_idx = gr.State([])

            # Fonksiyonlar
            def update_hist(txt, h_type):
                df, raw = get_filtered_history(txt, h_type)
                return df, raw, df.values.tolist()

            # Detail showing
            
            def show_det(evt: gr.SelectData, raw, h_type):
                if not raw or evt.index[0] >= len(raw): return None, "Seçim yok", []
                
                item = raw[evt.index[0]]
                
                # Görsel İşlemi (Aynı kalacak)
                img = None
                if h_type == "Arama Geçmişi" and item.get("image_b64"):
                    try: img = Image.open(io.BytesIO(base64.b64decode(item.get("image_b64"))))
                    except: pass
                
                # --- HTML TASARIMI OLUŞTURMA ---
                html_content = ""

                if h_type == "Arama Geçmişi":
                    # === TASARIM 1: ARAMA GEÇMİŞİ (ZENGİNLEŞTİRİLMİŞ) ===
                    query = item.get('query', '-')
                    timestamp = item.get('timestamp', '-')
                    results = item.get('full_results', [])
                    
                    # Üst Bilgi Alanı
                    html_content = f"""
                    <div style="font-family: 'Segoe UI', sans-serif; padding: 5px;">
                        <div style="background: linear-gradient(to right, #ece9e6, #ffffff); padding: 15px; border-radius: 8px; border-left: 5px solid #3498db; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05);">
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <div>
                                    <div style="color: #7f8c8d; font-size: 0.85em; margin-bottom: 5px; text-transform:uppercase; letter-spacing:1px;">📅 Arama Zamanı: {timestamp}</div>
                                    <div style="font-size: 1.4em; color: #2c3e50;">🔍 Aranan: <strong style="color:#2980b9;">{query}</strong></div>
                                </div>
                                <div style="background:#3498db; color:white; padding:5px 12px; border-radius:15px; font-weight:bold; font-size:0.9em;">
                                    {len(results)} Sonuç
                                </div>
                            </div>
                        </div>
                        <h4 style="margin-bottom: 15px; color: #34495e; border-bottom: 2px solid #eee; padding-bottom: 8px;">Bulunan Emsaller</h4>
                    """
                    
                    # Sonuç Kartları
                    if results:
                        for res in results:
                            p_name = res.get('product_name', 'İsimsiz')
                            gtip = res.get('assigned_gtip', '-')
                            # İçerik bilgisi varsa al, yoksa tire koy
                            comp = res.get('composition_text', res.get('composition', '-'))
                            # Özet gerekçe varsa al
                            reason = res.get('short_reason', '-')

                            html_content += f"""
                            <div style="background: white; border: 1px solid #e0e0e0; padding: 15px; margin-bottom: 15px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.04); transition: transform 0.2s;">
                                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; border-bottom: 1px solid #f0f0f0; padding-bottom: 8px;">
                                    <span style="color: #2c3e50; font-weight: 700; font-size: 1.1em;">{p_name}</span>
                                    <span style="background: #e8f6f3; color: #16a085; padding: 4px 10px; border-radius: 6px; font-size: 0.95em; font-weight: bold; border: 1px solid #d1f2eb;">{gtip}</span>

                                </div>
                                
                                <div style="margin-bottom: 8px; font-size: 0.95em; color: #444;">
                                    <strong style="color:#e67e22;">🧪 İçerik:</strong> {comp[:150] + ('...' if len(str(comp))>150 else '')}
                                </div>
                                
                                <div style="background: #f9f9f9; padding: 8px; border-radius: 5px; font-size: 0.9em; color: #666; font-style: italic; border-left: 3px solid #bdc3c7;">
                                    💡 {reason}
                                </div>
                            </div>
                            """
                    else:
                        html_content += "<div style='color:#999; font-style:italic; padding:10px; text-align:center;'>Kayıtlı sonuç bulunamadı.</div>"
                    
                    html_content += "</div>"

                elif h_type == "Kaydedilen Emsaller":
                    # === TASARIM 2: DETAYLI EMSAL KARTI GÖRÜNÜMÜ ===
                    p_name = item.get('product_name', 'Ürün Adı Yok')
                    gtip = item.get('assigned_gtip', 'Belirlenmemiş')
                    comp = item.get('composition_text', '-')
                    features = item.get('features', {})
                    use = features.get('use', '-') if features else '-'
                    reason = item.get('short_reason', 'Gerekçe girilmemiş.')
                    date = item.get('assignment_date', '-')
                    
                    # Teknik detay tablosu (features içindeki diğer veriler)
                    tech_rows = ""
                    if features:
                        for k, v in features.items():
                            if k != 'use' and v is not None:
                                val_display = "Evet" if v is True else ("Hayır" if v is False else v)
                                tech_rows += f"<tr><td style='padding:6px; border-bottom:1px solid #eee; color:#666;'>{k}</td><td style='padding:6px; border-bottom:1px solid #eee; color:#333;'>{val_display}</td></tr>"

                    html_content = f"""
                    <div style="font-family:'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; border:1px solid #dcdcdc; border-radius:10px; overflow:hidden; background:white; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
                        
                        <div style="background: linear-gradient(135deg, #6EA9E4  20%, #1565C0 80%); color:white; padding:20px;">
                            <h2 style="margin:0; font-size:1.4em; letter-spacing:0.5px;">{p_name}</h2>
                            <div style="margin-top:8px; font-size:1.2em; background:rgba(255,255,255,0.2); display:inline-block; padding:4px 10px; border-radius:4px;">
                                GTIP: <strong>{gtip}</strong>
                            </div>
                        </div>
                        
                        <div style="padding:20px;">
                            
                            <div style="margin-bottom:20px;">
                                <strong style="display:block; color:#e67e22; margin-bottom:5px; font-size:0.95em; text-transform:uppercase;">🧪 İçerik / Bileşim</strong>
                                <div style="background:#fdfefe; border:1px solid #ecf0f1; padding:10px; border-radius:6px; color:#34495e; line-height:1.5;">
                                    {comp}
                                </div>
                            </div>

                            <div style="margin-bottom:20px;">
                                <strong style="display:block; color:#27ae60; margin-bottom:5px; font-size:0.95em; text-transform:uppercase;">🏭 Kullanım Alanı</strong>
                                <div style="color:#333;">{use}</div>
                            </div>

                            <div style="margin-bottom:20px;">
                                <strong style="display:block; color:#8e44ad; margin-bottom:5px; font-size:0.95em; text-transform:uppercase;">📋 Sınıflandırma Gerekçesi</strong>
                                <div style="background:#f4ecf7; color:#5b2c6f; padding:12px; border-left:4px solid #8e44ad; border-radius:0 4px 4px 0; font-style:italic;">
                                    "{reason}"
                                </div>
                            </div>
                            
                            <details style="background:#fafafa; border:1px solid #eee; border-radius:6px; padding:8px;">
                                <summary style="cursor:pointer; font-weight:600; color:#555;">⚙️ Teknik Detaylar ve Özellikler</summary>
                                <table style="width:100%; margin-top:10px; border-collapse:collapse; font-size:0.9em;">
                                    {tech_rows}
                                </table>
                            </details>

                            <div style="margin-top:20px; text-align:right; font-size:0.8em; color:#bdc3c7;">
                                Kayıt ID: {item.get('id', '-')} • Tarih: {date}
                            </div>
                        </div>
                    </div>
                    """

                elif h_type == "Sınıflandırma Geçmişi":
                    p_name = item.get("product_name", "İsimsiz Ürün")
                    timestamp = item.get("timestamp", "-")
                    filename = item.get("filename", "Dosya belirtilmemiş")
                    composition = item.get("composition", "İçerik bilgisi yok.")
                    ai_html = item.get("ai_response", "<p>Detaylı analiz bulunamadı.</p>")
                    
                    html_content = f"""
                    <div style="font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; border: 1px solid #e0e0e0; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 25px rgba(0,0,0,0.05); background: #ffffff;">
                        
                        <div style="background: linear-gradient(135deg, #AF6CEA 40%, #8e2de2 60%); padding: 25px; color: white;">
                            <div style="display:flex; justify-content:space-between; align-items:start;">
                                <div>
                                    <div style="color: #ffffff; font-size: 0.95em; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 5px; font-weight: 600;">🧬 Sınıflandırma Raporu</div>
                                    <h2 style="margin: 0; font-size: 1.6em; font-weight: 600; text-shadow: 0 2px 4px rgba(0,0,0,0.2);">{p_name}</h2>
                                </div>
                                <div style="text-align:right;">
                                    <span style="background: rgba(255,255,255,0.7); backdrop-filter: blur(5px); padding: 5px 12px; border-radius: 20px; font-size: 0.85em; display: inline-flex; align-items: center; gap:5px;">
                                        📅 {timestamp}
                                    </span>
                                </div>
                            </div>
                            
                            <div style="margin-top: 20px; display: flex; flex-wrap: wrap; gap: 10px;">
                                <span style="background: rgba(255,255,255,0.7); padding: 4px 12px; border-radius: 8px; font-size: 0.9em; border: 1px solid rgba(255,255,255,0.1);">
                                    📎 <strong>Dosya:</strong> {filename}
                                </span>
                            </div>
                        </div>

                        <div style="background: #f9fafb; padding: 15px 25px; border-bottom: 1px solid #eee;">
                            <strong style="color: #555; font-size: 0.9em; display:block; margin-bottom:5px;">🧪 Tanımlanan İçerik:</strong>
                            <div style="color: #333; font-size: 0.95em; line-height: 1.4;">{composition}</div>
                        </div>

                        <div style="padding: 30px;">
                            <div style="margin-bottom: 20px; border-left: 4px solid #8E2DE2; padding-left: 15px;">
                                <h3 style="margin: 0; color: #2c3e50; font-size: 1.3em;">Detaylı AI Analizi</h3>
                                <small style="color: #7f8c8d;">Gemini Model Çıktısı</small>
                            </div>
                            
                            <div style="font-size: 1em; line-height: 1.7; color: #2c3e50;">
                                {ai_html}
                            </div>
                        </div>
                        
                        <div style="background: #f1f2f6; padding: 10px 25px; text-align: right; border-top: 1px solid #e0e0e0;">
                            <small style="color: #bdc3c7;">GTIP Asistanı v1.0 • Otomatik Üretilmiştir</small>
                        </div>
                    </div>
                    """

                return gr.update(value=img, visible=bool(img)), html_content, [evt.index[0]]
            
            def del_sel(idxs, view, h_type):
                """Seçili satırları siler (Backend fonksiyonunu çağırır)."""
                # Daha önce yazdığımız 'delete_selected_history_items' fonksiyonunu kullanır
                df, raw = delete_selected_history_items(idxs, view, h_type)
                # Tabloyu, ham veriyi güncelle; Detayları sıfırla
                return df, raw, df.values.tolist(), None, "", []

            def del_all(h_type):
                """Seçili moda göre tüm geçmişi siler."""
                target_file = None
                
                # Hangi moddaysak o dosyayı hedef al
                if h_type == "Arama Geçmişi":
                    target_file = SEARCH_LOG_FILE
                elif h_type == "Sınıflandırma Geçmişi":
                    target_file = CLASSIFICATION_LOG_FILE
                
                # Dosya varsa sil
                if target_file and os.path.exists(target_file):
                    try: 
                        os.remove(target_file)
                    except Exception as e: 
                        print(f"Silme hatası: {e}")
                
                # Tabloyu yenile (Boş dönecektir)
                df, raw = get_filtered_history(history_type=h_type)
                return df, raw, df.values.tolist(), None, "", []

            # Eventler
            hist_refresh.click(update_hist, [hist_filter, hist_type_selector], [hist_table, hist_raw, hist_view])
            hist_filter.change(update_hist, [hist_filter, hist_type_selector], [hist_table, hist_raw, hist_view])
            hist_type_selector.change(update_hist, [hist_filter, hist_type_selector], [hist_table, hist_raw, hist_view])
            
            hist_table.select(show_det, [hist_raw, hist_type_selector], [det_img, det_html, sel_idx])
            
            # Seçileni Sil Butonu
            hist_del_sel.click(
                fn=del_sel, 
                inputs=[sel_idx, hist_view, hist_type_selector], # <-- Buraya h_type eklendi
                outputs=[hist_table, hist_raw, hist_view, det_img, det_html, sel_idx]
            )

            # Tümünü Sil Butonu
            hist_del_all.click(
                fn=del_all, 
                inputs=[hist_type_selector], # <-- Sadece h_type yeterli
                outputs=[hist_table, hist_raw, hist_view, det_img, det_html, sel_idx]
            )

        with gr.TabItem("Hakkında"):
            gr.Markdown("## 📚 Kullanım Kılavuzu ve Hakkında")
            
            with gr.Accordion("1. Emsal Arama (Akıllı Arama)", open=True):
                gr.Markdown("""
                * **Akıllı Arama:** Ürün adı, marka veya kimyasal içerik yazın. Sistem yazım hatalarını tolere eder.
                * **Fotoğraflı Arama:** SDS veya etiket fotoğrafını yükleyip "Fotoğrafı Oku" butonuna basarak metni otomatik doldurun.
                """)
            
            with gr.Accordion("2. Yeni Emsal Ekle (Görsel Analiz)", open=True):
                gr.Markdown("""
                * Elinizdeki GTIP Tespit Formu (veya SDS) görselini yükleyin.
                * **"Analiz Et ve Ekle"** butonuna basın. Yapay zeka verileri okur ve veritabanına (`cases.jsonl`) ekler.
                """)
            
            with gr.Accordion("3. Sınıflandırma Asistanı (Yapay Zeka Yorumu)", open=True):
                gr.Markdown("""
                * Veritabanında olmayan yeni bir ürün için yapay zekadan görüş alın.
                * Ürün bilgilerini girin veya SDS fotoğrafı yükleyin.
                * Asistan, **Devlet Fasılları** ve **Benzer Emsallere** dayanarak resmi bir yorum yazar.
                """)

            with gr.Accordion("4. Ayarlar", open=True):
                gr.Markdown("""
                * Google Gemini API Anahtarını giriniz.
                * Uygun modelleri listeleyiniz.
                * Düşünebilen yapay zeka için **Pro**, daha hızlı yanıtlar için **Flash** modellerini tercih edebilirsiniz.
                """)

            with gr.Accordion("5. Geçmiş Aramalar", open=False):
                gr.Markdown("""
                * Yaptığınız tüm aramalar (fotoğraflar dahil) burada saklanır.
                * Eski aramaları ve sınıflandırma kayıtlarını tekrar görüntüleyebilirsiniz.
                * Gereksiz kayıtları silebilirsiniz.
                """)
            
            with gr.Accordion("6. Vergi Asistanı", open=False):
                gr.Markdown("""
                * Yönetici paneli kısmından aralıklarla güncellenen vergi listesini yükleyebilirsiniz.
                * Elinizdeki ürün listesini **Sipariş Listesi** olarak yükleyiniz.
                * Bileşenlerin SDS/MSDS Bilgilerini içeren dosyayı **Bileşen Detay Listesi** olarak yükleyiniz.
                * Sonuç Raporunu hazır olunca indirebilirsiniz.
                """)

            gr.Markdown("<br><br>") 
            gr.HTML("""
            <div style="text-align: center; opacity: 0.6; font-size: 0.85em; font-family: sans-serif; color: #666; margin-top: 20px; border-top: 1px solid #eee; padding-top: 10px;">
                <p style="margin-bottom: 4px;"><strong>Geliştiriciler:</strong> <span style="color: #2196F3;">Emre Ongan</span> & <span style="color: #2196F3;">Bekir Can Yalçın</span></p>
                <p style="margin-top: 0;"><small>Katkılarıyla: <strong>Ayça Biçen</strong></small></p>
                <div style="font-size: 0.7em; color: #ccc; margin-top: 5px;">© 2025 GTIP Asistanı v1.0</div>
            </div>
            """)
            

        # === SEKME: VERGİ ASİSTANI (YENİ) ===
        with gr.TabItem("Vergi Asistanı"):
            gr.Markdown("### 🏛️ Gümrük Vergisi ve Muafiyet Analizi")
            
            # --- YÖNETİCİ PANELİ (AYNI KALIYOR) ---
            with gr.Accordion("⚙️ Yönetici Paneli: Vergi Listesi Güncelleme (V Sayılı Liste)", open=False):
                gr.Markdown("""
                Devlet tarafından yayınlanan **V Sayılı Liste** Excel dosyasını buradan yükleyip sistemi güncelleyebilirsiniz.
                """)
                with gr.Row():
                    with gr.Column(scale=3):
                        tax_file_input = gr.File(label="Güncel Vergi Listesi (.xlsx)", file_types=[".xlsx", ".xls"])
                    with gr.Column(scale=1):
                        tax_update_btn = gr.Button("Listeyi Sisteme İşle 💾", variant="primary")
                
                tax_status_output = gr.Textbox(label="İşlem Durumu", value=get_tax_db_status(), interactive=False)
                tax_refresh_btn = gr.Button("Durumu Yenile", size="sm")

                tax_update_btn.click(process_and_save_tax_excel, inputs=[tax_file_input], outputs=[tax_status_output])
                tax_refresh_btn.click(get_tax_db_status, inputs=[], outputs=[tax_status_output])

            gr.Markdown("---")
            
            # --- YENİ ANALİZ BÖLÜMÜ ---
            gr.Markdown("### 🚀 Otomatik Ürün & Bileşen Analizi")
            gr.Markdown("Sipariş listesini ve ilgili bileşen (SDS) listesini yükleyin. Sistem ürünlerin içeriğindeki maddeleri vergi listesinde tarar.")

            with gr.Row():
                with gr.Column(scale=1):
                    # 1. Input: Sipariş Listesi
                    order_list_input = gr.File(
                        label="1. Sipariş Listesi (Excel/CSV)", 
                        file_types=[".xlsx", ".csv"],
                        height=100
                    )
                    gr.Markdown("<sub>*İçinde 'Malzeme' sütunu olmalı.*</sub>")
                    
                    # 2. Input: Bileşen Listesi
                    ing_list_input = gr.File(
                        label="2. Bileşen Detay Listesi (Excel/CSV)", 
                        file_types=[".xlsx", ".csv"],
                        height=100
                    )
                    gr.Markdown("<sub>*Type(*), Product code, CAS, Percent sütunları olmalı.*</sub>")
                    
                    analyze_excel_btn = gr.Button("Eşleştir ve Analiz Et 📊", variant="primary")
                
                with gr.Column(scale=1):
                    # Çıktılar
                    analysis_log = gr.HTML(label="İşlem Durumu")
                    analysis_output_file = gr.File(label="Sonuç Raporu (.xlsx)")

            # Buton Aksiyonu
            analyze_excel_btn.click(
                fn=process_tax_analysis_structured,
                inputs=[order_list_input, ing_list_input],
                outputs=[analysis_log, analysis_output_file]
            )



gradio_app = gr.mount_gradio_app(fastapi_app, gradio_ui, path="/")

if __name__ == "__main__":
    print("Uygulama Başlatılıyor...")
    try: webbrowser.open("http://127.0.0.1:7860")
    except: pass
    uvicorn.run(gradio_app, host="127.0.0.1", port=7860)