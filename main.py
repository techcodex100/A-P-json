import re
import os
import tempfile
import string
from typing import List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from PyPDF2 import PdfReader
import pandas as pd

app = FastAPI(title="PDF → Normalized JSON CSV")

CSV_FILE_PATH = "json.csv"

# ---------- Helpers ----------
def safe_search(pattern: str, text: str, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None

def clean_text(text: str):
    if not text:
        return ""
    allowed = string.ascii_letters + string.digits + " .,/-"
    return "".join(c for c in text if c in allowed)

def extract_text_from_pdf(path: str) -> str:
    reader = PdfReader(path)
    return "\n".join([p.extract_text() or "" for p in reader.pages])

def extract_agreement_data(pdf_path: str):
    text = extract_text_from_pdf(pdf_path)
    txt = re.sub(r"\r", "\n", text)
    txt = re.sub(r"[ \t]+", " ", txt)

    prod_match = re.search(
        r"([A-Za-z0-9 &\-]+?)\s+([0-9,]+\s*(?:bales|units|kg)?)\s+([\d,\.]+\s*USD(?:\/\w+)?)\s+([\d,\.]+\s*USD)",
        txt,
        re.I,
    )
    if prod_match:
        product_name = clean_text(prod_match.group(1).strip())
        product_quantity = clean_text(prod_match.group(2).strip())
        product_price = clean_text(prod_match.group(3).strip())
        product_amount = clean_text(prod_match.group(4).strip())
    else:
        product_name = product_quantity = product_price = product_amount = None

    return {
        "contract_no": clean_text(safe_search(r"Contract No[:\s]*([A-Z0-9\-\./]+)", txt, re.I)),
        "date": clean_text(safe_search(r"Date[:\s]*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})", txt, re.I)),
        "website": clean_text(safe_search(r"Website[:\s]*([^\n]+)", txt, re.I)),
        "email": clean_text(safe_search(r"Email[:\s]*([^\n]+)", txt, re.I)),
        "company_name": clean_text(safe_search(r"([A-Z][A-Z0-9 &]*PVT LTD)", txt, re.M)),
        "address": clean_text(safe_search(r"Address[:\s]*([^\n]+)", txt, re.I)),
        "gst": clean_text(safe_search(r"GST[:\s]*([^\n]+)", txt, re.I) or safe_search(r"GSTIN([0-9A-Z]+)", txt, re.I)),
        "seller": clean_text(safe_search(r"SELLER\s+([\s\S]+?)\s+CONSIGNEE", txt, re.I)),
        "consignee": clean_text(safe_search(r"CONSIGNEE\s+([\s\S]+?)\s+NOTIFY PARTY", txt, re.I)),
        "notify_party": clean_text(safe_search(r"NOTIFY PARTY\s+([\s\S]+?)\s+Product", txt, re.I) or safe_search(r"NOTIFY PARTY\s+([\s\S]+)$", txt, re.I)),
        "product_name": product_name,
        "product_quantity": product_quantity,
        "product_price": product_price,
        "product_amount": product_amount,
        "packing": clean_text(safe_search(r"Packing[:\s]*([^\n]+)", txt, re.I)),
        "loading_port": clean_text(safe_search(r"Loading Port[:\s]*([^\n]+)", txt, re.I)),
        "destination_port": clean_text(safe_search(r"Destination Port[:\s]*([^\n]+)", txt, re.I)),
        "shipment_date": clean_text(safe_search(r"Shipment[:\s]*([^\n]+)", txt, re.I)),
        "seller_bank": clean_text(safe_search(r"Seller(?:’|')?s Bank[:\s]*([^\n]+)", txt, re.I)),
        "account_no": clean_text(safe_search(r"Account No\.?:\s*([0-9\-\s]+)", txt, re.I)),
        "documents": clean_text(safe_search(r"Documents[:\s]*([^\n]+)", txt, re.I)),
        "payment_terms": clean_text(safe_search(r"Payment Terms[:\s]*([^\n]+)", txt, re.I)),
    }

# ---------- Update CSV with Match Row ----------
def update_csv_with_json(pdf_jsons):
    normalized = pd.json_normalize(pdf_jsons)
    normalized.insert(0, "PDF_File", [f"PDF_{i+1}" for i in range(len(pdf_jsons))])

    match_row = {}
    match_message = "No comparison"
    
    if len(pdf_jsons) >= 2:
        json1, json2 = pdf_jsons[0], pdf_jsons[1]
        for col in normalized.columns:
            if col == "PDF_File":
                match_row[col] = "Match"
            else:
                v1 = str(json1.get(col, "")).strip()
                v2 = str(json2.get(col, "")).strip()
                match_row[col] = 1 if v1 == v2 and v1 != "" else 0

        # decide overall match message
        values = [v for k, v in match_row.items() if k != "PDF_File"]
        if all(v == 1 for v in values):
            match_message = "Successful Match"
        else:
            match_message = "Unsuccessful Match"

    # Append match row if created
    if match_row:
        combined_df = pd.concat([normalized, pd.DataFrame([match_row])], ignore_index=True)
    else:
        combined_df = normalized

    combined_df.to_csv(CSV_FILE_PATH, index=False, encoding="utf-8")
    return match_message

# ---------- Routes ----------
@app.post("/upload-two-pdfs/")
async def upload_two_pdfs(files: List[UploadFile] = File(...)):
    if len(files) != 2:
        raise HTTPException(status_code=400, detail="Upload exactly 2 PDF files.")

    pdf_jsons = []
    for file in files:
        if not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"{file.filename} is not a PDF.")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        try:
            content = await file.read()
            tmp.write(content)
            tmp.close()
            data = extract_agreement_data(tmp.name)
            pdf_jsons.append(data)
        finally:
            os.remove(tmp.name)

    update_csv_with_json(pdf_jsons)  # Save normalized JSON to CSV

    return {
        "message": "CSV Updated with Match Row",
        "csv_path": CSV_FILE_PATH
    }

@app.get("/get-csv/")
async def get_csv():
    if not os.path.exists(CSV_FILE_PATH):
        raise HTTPException(status_code=404, detail="CSV file not found.")
    return FileResponse(CSV_FILE_PATH, media_type="text/csv", filename="json.csv")
